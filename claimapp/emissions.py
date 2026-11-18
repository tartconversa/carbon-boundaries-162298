"""减排量计算：份额界限、共享因子、边界口径与可识别范围。

所有结果都是区间。计算只在同一证据版本内进行；不同边界、年限、生物源处理
或证据版本的结果不相互换算，也不默认以某个原用途为全量假设。
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass

from scipy.optimize import linprog

from .records import DataError, Dataset

EVIDENCE_INITIAL = "initial"


def dry_tonnes(lot) -> float:
    return lot.wet_t * (1.0 - lot.moisture_pct / 100.0)


def _stamp(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value)


def correction_state(dataset: Dataset, as_of: str | None = None) -> dict:
    """按可知时刻返回每条原用途记录被替换后的份额界限。

    as_of 为 None 时纳入资料包内全部更正；否则只纳入 available_at <= as_of 的更正。
    最新可知的更正覆盖较早已可知的更正；其余原用途记录保持原始界限。
    """
    limit = _stamp(as_of) if as_of is not None else None
    applied = {}
    for correction in sorted(dataset.corrections, key=lambda c: (_stamp(c.available_at), c.correction_id)):
        if limit is not None and _stamp(correction.available_at) > limit:
            continue
        applied[correction.record_id] = correction
    result = {}
    for option in dataset.baseline_options:
        correction = applied.get(option.record_id)
        if correction is None:
            result[option.record_id] = {
                "share_min": option.share_min,
                "share_max": option.share_max,
                "correction_ids": [],
            }
        else:
            result[option.record_id] = {
                "share_min": correction.share_min,
                "share_max": correction.share_max,
                "correction_ids": [correction.correction_id],
            }
    return result


def evidence_label(dataset: Dataset, as_of: str | None = None) -> dict:
    limit = _stamp(as_of) if as_of is not None else None
    ids = []
    latest = None
    for correction in dataset.corrections:
        if limit is not None and _stamp(correction.available_at) > limit:
            continue
        ids.append(correction.correction_id)
        stamp = correction.available_at
        latest = stamp if latest is None or stamp > latest else latest
    return {
        "as_of": as_of,
        "corrections_applied": sorted(ids),
        "vintage": latest or EVIDENCE_INITIAL,
    }


def _gross_extreme(components, which: str):
    """在份额单纯形与上下界内求基线排放系数的极小或极大值（线性规划）。"""
    import numpy as np

    if not components:
        raise DataError("原料没有原用途登记")
    coefficients = [getattr(c, "factor_" + which) for c in components]
    lower = [c.share_min for c in components]
    upper = [c.share_max for c in components]
    if sum(lower) > 1 + 1e-9 or sum(upper) < 1 - 1e-9:
        raise DataError("原用途份额界限无法同时成立")
    sense = 1.0 if which == "max" else -1.0
    result = linprog(
        [sense * value for value in coefficients],
        A_eq=[[1.0] * len(components)],
        b_eq=[1.0],
        bounds=list(zip(lower, upper)),
        method="highs",
    )
    if not result.success:
        raise DataError("原用途份额界限无法求解")
    shares = [0.0 if abs(v) < 1e-12 else float(v) for v in result.x]
    value = sense * float(result.fun)
    return value, shares


@dataclass(frozen=True)
class Component:
    record_id: str
    use: str
    share_min: float
    share_max: float
    factor_min: float
    factor_max: float
    correction_ids: tuple


def lot_components(dataset: Dataset, lot_id: str, as_of: str | None = None) -> tuple:
    state = correction_state(dataset, as_of)
    result = []
    for option in dataset.baseline_for(lot_id):
        bounds = state[option.record_id]
        result.append(
            Component(
                record_id=option.record_id,
                use=option.use,
                share_min=bounds["share_min"],
                share_max=bounds["share_max"],
                factor_min=option.factor_min,
                factor_max=option.factor_max,
                correction_ids=tuple(bounds["correction_ids"]),
            )
        )
    return tuple(result)


def baseline_extremes(dataset: Dataset, lot_id: str, as_of: str | None = None) -> dict:
    """原去向基线的可识别区间（千克二氧化碳当量，已乘干吨）。

    factor 为每干吨系数，份额界限只约束比例；上下界分别求解，不取同一组份额。
    """
    components = lot_components(dataset, lot_id, as_of)
    dry = dry_tonnes(dataset.lot(lot_id))
    per_tonne_low, low_shares = _gross_extreme(components, "min")
    per_tonne_high, high_shares = _gross_extreme(components, "max")
    return {
        "components": components,
        "dry_t": dry,
        "per_dry_tonne": {"min": per_tonne_low, "max": per_tonne_high},
        "gross_min": per_tonne_low * dry,
        "gross_max": per_tonne_high * dry,
        "gross_min_shares": {c.use: round(s, 10) for c, s in zip(components, low_shares)},
        "gross_max_shares": {c.use: round(s, 10) for c, s in zip(components, high_shares)},
    }


def _factor_range(dataset: Dataset, group: str, kind: str) -> dict:
    matches = [f for f in dataset.factors if f.assumption_group == group and f.kind == kind]
    if len(matches) != 1:
        raise DataError(f"共享组 {group} 缺少唯一 {kind} 因子")
    factor = matches[0]
    return {"factor_id": factor.factor_id, "min": factor.min, "max": factor.max}


def lot_inputs(dataset: Dataset, lot_id: str) -> dict:
    lot = dataset.lot(lot_id)
    dry = dry_tonnes(lot)
    return {
        "lot_id": lot.lot_id,
        "wet_t": lot.wet_t,
        "moisture_pct": lot.moisture_pct,
        "dry_t": dry,
        "processing_kwh": lot.processing_kwh,
        "transport_km": lot.transport_km,
        "assumption_group": lot.assumption_group,
        "grid": _factor_range(dataset, lot.assumption_group, "grid"),
        "transport": _factor_range(dataset, lot.assumption_group, "transport"),
        "biogenic": _factor_range(dataset, lot.assumption_group, "biogenic"),
    }


def direct_extremes(dataset: Dataset, lot_id: str, boundary, as_of: str | None = None) -> dict:
    """按边界计入的本地直接排放区间（加工、运输、生物源）。"""
    inputs = lot_inputs(dataset, lot_id)
    dry = inputs["dry_t"]
    processing = None
    if boundary.include_processing:
        processing = {
            "min": inputs["processing_kwh"] * inputs["grid"]["min"],
            "max": inputs["processing_kwh"] * inputs["grid"]["max"],
            "formula": "processing_kwh × grid",
        }
    transport = None
    if boundary.include_transport:
        base = inputs["wet_t"] * inputs["transport_km"]
        transport = {
            "min": base * inputs["transport"]["min"],
            "max": base * inputs["transport"]["max"],
            "formula": "wet_t × transport_km × transport",
        }
    biogenic = None
    if boundary.include_biogenic:
        biogenic = {
            "min": dry * inputs["biogenic"]["min"],
            "max": dry * inputs["biogenic"]["max"],
            "formula": "dry_t × biogenic",
        }
    low = sum(part["min"] for part in (processing, transport, biogenic) if part)
    high = sum(part["max"] for part in (processing, transport, biogenic) if part)
    return {
        "processing": processing,
        "transport": transport,
        "biogenic": biogenic,
        "min": low,
        "max": high,
        "included": {
            "processing": bool(processing),
            "transport": bool(transport),
            "biogenic": bool(biogenic),
        },
    }


def allocation_weight(output, boundary) -> float | None:
    if boundary.allocation == "energy":
        return output.energy_share
    if boundary.allocation == "value":
        return output.value_share
    if boundary.allocation == "system":
        return None
    raise DataError("未知分配约定：" + boundary.allocation)


def substitution_total(dataset: Dataset, lot_id: str) -> dict:
    outputs = [o for o in dataset.outputs.values() if o.lot_id == lot_id]
    total = sum(o.substitute_factor * float(o.quantity) for o in outputs)
    return {
        "substitution_kg": total,
        "outputs": {
            o.output_id: {
                "substitute_factor": o.substitute_factor,
                "quantity": float(o.quantity),
                "unit": o.unit,
                "credit_share": (o.substitute_factor * float(o.quantity) / total) if total else 0.0,
            }
            for o in outputs
        },
    }


def output_range(dataset: Dataset, output_id: str, boundary, as_of: str | None = None) -> dict:
    """某个产出在给定边界下的每单位及全量减排区间。

    区间用同一组共享端点计算：基线下界配直接排放上界，基线上界配直接排放下界。
    """
    output = dataset.output(output_id)
    lot_id = output.lot_id
    inputs = lot_inputs(dataset, lot_id)
    direct = direct_extremes(dataset, lot_id, boundary, as_of)
    extremes = baseline_extremes(dataset, lot_id, as_of)
    quantity = float(output.quantity)
    weight = allocation_weight(output, boundary)

    if weight is None:
        substitution = substitution_total(dataset, lot_id)
        total_sub = substitution["substitution_kg"]
        if total_sub <= 0:
            raise DataError("替代总量必须为正：" + lot_id)
        net_low = total_sub - direct["max"]
        net_high = total_sub - direct["min"]
        factor = output.substitute_factor
        per_unit_low = factor * net_low / total_sub
        per_unit_high = factor * net_high / total_sub
        basis = "system: 每单位产出替代量 × 全链净替代比例"
        weight_value = None
    else:
        net_low = extremes["gross_min"] - direct["max"]
        net_high = extremes["gross_max"] - direct["min"]
        per_unit_low = weight * net_low / quantity
        per_unit_high = weight * net_high / quantity
        basis = f"{boundary.allocation}: 权重 × （基线 − 直接排放）/ 产出数量"
        weight_value = weight

    return {
        "output_id": output.output_id,
        "lot_id": lot_id,
        "product": output.product,
        "unit": output.unit,
        "quantity": quantity,
        "allocation_weight": weight_value,
        "per_unit_kg": {"min": per_unit_low, "max": per_unit_high},
        "total_kg": {"min": per_unit_low * quantity, "max": per_unit_high * quantity},
        "baseline_kg": {"min": extremes["gross_min"], "max": extremes["gross_max"]},
        "direct_kg": {"min": direct["min"], "max": direct["max"]},
        "basis": basis,
    }


def lot_total(dataset: Dataset, lot_id: str, boundary, as_of: str | None = None) -> dict:
    """同一来源、同一边界下按全部真实产出解释的全链总量区间。"""
    outputs = [o for o in dataset.outputs.values() if o.lot_id == lot_id]
    parts = []
    total_low = total_high = 0.0
    for output in outputs:
        part = output_range(dataset, output.output_id, boundary, as_of)
        parts.append(part)
        total_low += part["total_kg"]["min"]
        total_high += part["total_kg"]["max"]
    if boundary.allocation == "system":
        method = "system: 各产出真实数量对应的替代量合计 − 直接排放"
    else:
        method = f"{boundary.allocation}: 权重合计为 1，总量与产出拆分无关"
    return {
        "lot_id": lot_id,
        "boundary_id": boundary.boundary_id,
        "method": method,
        "total_kg": {"min": total_low, "max": total_high},
        "outputs": parts,
    }


def comparability(boundary_a, boundary_b, evidence_a: dict, evidence_b: dict) -> dict:
    """两个口径或证据版本能否相加或排序。"""
    reasons = []
    if boundary_a.allocation != boundary_b.allocation:
        reasons.append(f"分配约定不同（{boundary_a.allocation} vs {boundary_b.allocation}）")
    if boundary_a.horizon_years != boundary_b.horizon_years:
        reasons.append(f"时间范围不同（{boundary_a.horizon_years} vs {boundary_b.horizon_years} 年）")
    if boundary_a.include_biogenic != boundary_b.include_biogenic:
        reasons.append("生物源处理不同")
    if (boundary_a.include_processing, boundary_a.include_transport) != (
        boundary_b.include_processing,
        boundary_b.include_transport,
    ):
        reasons.append("加工或运输计入范围不同")
    if evidence_a["vintage"] != evidence_b["vintage"]:
        reasons.append(f"证据版本不同（{evidence_a['vintage']} vs {evidence_b['vintage']}）")
    return {"comparable": not reasons, "reasons": reasons}


def basis_key(boundary, evidence: dict) -> tuple:
    return (
        boundary.allocation,
        boundary.include_processing,
        boundary.include_transport,
        boundary.include_biogenic,
        boundary.horizon_years,
        evidence["vintage"],
    )


def round_range(range_: dict) -> dict:
    return {key: round(value, 6) for key, value in range_.items()}
