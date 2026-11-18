"""把多份声明放回一致边界核对，并处理共享假设造成的相关性。

同一 assumption_group 的运输、电网和生物源因子是共同参数。各客户分别取
对自己最有利的一端再相加，会得到一个无法同时成立的合计；这里同时给出
逐份声明各自区间之和，以及共享端点下的联合可实现区间。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import emissions, ledger
from .records import Dataset


@dataclass
class _Piece:
    claim_id: str
    boundary_id: str
    output_id: str
    lot_id: str
    group: str
    boundary_id: str
    vintage: str
    quantity: float
    allocation: str
    coefficient: float       # 乘在基线与直接排放上的系数
    constant: float          # system 口径下替代量固定项
    direct: dict             # processing / transport / biogenic 的直接排放系数


def _piece(dataset: Dataset, claim, as_of: str | None) -> _Piece:
    output = dataset.output(claim.output_id)
    lot = dataset.lot(output.lot_id)
    boundary = dataset.boundary(claim.boundary_id)
    evidence = emissions.evidence_label(dataset, as_of)
    quantity = float(claim.quantity)
    inputs = emissions.lot_inputs(dataset, lot.lot_id)
    if boundary.allocation == "system":
        total_sub = emissions.substitution_total(dataset, lot.lot_id)["substitution_kg"]
        coefficient = output.substitute_factor * quantity / total_sub
        constant = coefficient * total_sub
    else:
        weight = emissions.allocation_weight(output, boundary)
        coefficient = weight * quantity / float(output.quantity)
        constant = 0.0
    direct = {}
    if boundary.include_processing:
        direct["grid"] = coefficient * inputs["processing_kwh"]
    if boundary.include_transport:
        direct["transport"] = coefficient * inputs["wet_t"] * inputs["transport_km"]
    if boundary.include_biogenic:
        direct["biogenic"] = coefficient * inputs["dry_t"]
    return _Piece(
        claim_id=claim.claim_id,
        boundary_id=boundary.boundary_id,
        output_id=output.output_id,
        lot_id=lot.lot_id,
        group=lot.assumption_group,
        vintage=evidence["vintage"],
        quantity=quantity,
        allocation=boundary.allocation,
        coefficient=coefficient,
        constant=constant,
        direct=direct,
    )


def _joint(pieces: list, dataset: Dataset, as_of: str | None) -> dict:
    """共享端点下的联合区间。

    同一 assumption_group 的共同因子只能取一个值，同一原料的基线份额也必须
    落在同一组界限内；因此不能把每份声明各自的端点直接相加。
    """
    base_per_lot = {}
    for piece in pieces:
        bucket = base_per_lot.setdefault((piece.lot_id, piece.vintage), {"up": 0.0, "down": 0.0})
        if piece.coefficient >= 0:
            bucket["up"] += piece.coefficient
        else:
            bucket["down"] += piece.coefficient
    constant_total = sum(p.constant for p in pieces)

    per_group_coeff = {}
    for piece in pieces:
        bucket = per_group_coeff.setdefault(piece.group, {})
        for kind, value in piece.direct.items():
            bucket[kind] = bucket.get(kind, 0.0) + value

    direct_min = direct_max = 0.0
    for group, kinds in per_group_coeff.items():
        for kind, coefficient in kinds.items():
            factor = dataset.group_factors(group)[kind]
            if coefficient >= 0:
                direct_min += coefficient * factor.min
                direct_max += coefficient * factor.max
            else:
                direct_min += coefficient * factor.max
                direct_max += coefficient * factor.min

    base_min = base_max = 0.0
    for (lot_id, vintage), weights in base_per_lot.items():
        extremes = emissions.baseline_extremes(dataset, lot_id, as_of)
        low, high = extremes["gross_min"], extremes["gross_max"]
        base_min += weights["up"] * low + weights["down"] * high
        base_max += weights["up"] * high + weights["down"] * low

    return {
        "min": base_min + constant_total - direct_max,
        "max": base_max + constant_total - direct_min,
        "groups": sorted(per_group_coeff),
    }


def portfolio(dataset: Dataset, as_of: str | None = None) -> dict:
    """按边界与证据版本分组核对客户声明，指出不可相加的组合。"""
    pieces = [_piece(dataset, claim, as_of) for claim in dataset.claims]
    groups = {}
    for piece in pieces:
        groups.setdefault((piece.boundary_id, piece.vintage), []).append(piece)

    rows = []
    for (boundary_id, vintage), members in sorted(groups.items()):
        row_ranges = []
        boundary = dataset.boundary(boundary_id)
        for piece in members:
            unit_range = emissions.output_range(dataset, piece.output_id, boundary, as_of)
            row_ranges.append(
                {
                    "min": unit_range["per_unit_kg"]["min"] * piece.quantity,
                    "max": unit_range["per_unit_kg"]["max"] * piece.quantity,
                }
            )
        naive = {"min": sum(r["min"] for r in row_ranges), "max": sum(r["max"] for r in row_ranges)}
        joint = _joint(members, dataset, as_of)
        rows.append(
            {
                "boundary_id": boundary_id,
                "vintage": vintage,
                "claims": sorted(p.claim_id for p in members),
                "assumption_groups": sorted({p.group for p in members}),
                "naive_sum_of_favourable_ends_kg": emissions.round_range(naive),
                "joint_feasible_kg": emissions.round_range(
                    {"min": joint["min"], "max": joint["max"]}
                ),
                "favourable_end_overstatement_kg": emissions.round_range(
                    {
                        "min": naive["min"] - joint["min"],
                        "max": naive["max"] - joint["max"],
                    }
                ),
                "shared_parameter_note": (
                    "同一来源的基线份额是同一组约束；同一 assumption_group 的"
                    "运输、电网与生物源因子只能取一个共同值。joint_feasible_kg "
                    "按组内共同端点求解，是可与对外解释一致的合计。"
                ),
            }
        )

    all_naive = {"min": 0.0, "max": 0.0}
    for row in rows:
        all_naive["min"] += row["naive_sum_of_favourable_ends_kg"]["min"]
        all_naive["max"] += row["naive_sum_of_favourable_ends_kg"]["max"]

    same_direction = all(
        row["favourable_end_overstatement_kg"]["min"] >= -1e-9
        and row["favourable_end_overstatement_kg"]["max"] <= 1e-9
        for row in rows
    )
    if same_direction:
        interpretation = (
            "本资料内各声明对共享参数的响应方向一致（因子取上界时基线也取上界），"
            "因此逐份端点相加恰好等于联合可实现区间；这一相等是当前数据的结果，"
            "不是可以沿用的规则。若客户边界或整改数值使响应方向相反，逐份取端"
            "仍会高估合计，应以 joint_feasible_kg 为准。"
        )
    else:
        interpretation = (
            "逐份取最有利端点再相加会高估合计；对外的总量解释应使用 joint_feasible_kg。"
        )

    return {
        "as_of": as_of,
        "evidence": emissions.evidence_label(dataset, as_of),
        "by_boundary": rows,
        "naive_grand_total_kg": emissions.round_range(all_naive),
        "interpretation": interpretation,
        "note": (
            "不同边界或证据版本的合计不相加；naive_grand_total_kg 仅供识别超额解释，"
            "不是可对外引用的总量。"
        ),
        "unallocated": {
            output_id: position.as_dict()
            for output_id, position in ledger.positions(dataset).items()
        },
    }
