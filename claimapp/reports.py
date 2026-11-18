"""从声明、更正或原始产出出发的核对报告。

对外报告只描述本地合同口径，不宣称符合任何真实认证规则。
"""
from __future__ import annotations

from . import emissions, ledger, portfolio, statements
from .records import DataError, Dataset

DISCLAIMER = "本报告为本地合成资料的合同口径结果，不代表真实认证或核证结论。"


def inventory(dataset: Dataset) -> dict:
    positions = ledger.positions(dataset)
    over = ledger.overcommitments(dataset)
    return {
        "disclaimer": DISCLAIMER,
        "lots": [
            {
                "lot_id": lot_id,
                "assumption_group": lot.assumption_group,
                "dry_t": round(emissions.dry_tonnes(lot), 6),
                "outputs": [o.output_id for o in dataset.outputs.values() if o.lot_id == lot_id],
            }
            for lot_id, lot in dataset.feedstock.items()
        ],
        "positions": {key: value.as_dict() for key, value in positions.items()},
        "overcommitments": over,
        "incoming": ledger.settle_incoming(dataset),
        "evidence": emissions.evidence_label(dataset),
        "statements": [s.as_dict() for s in statements.statements(dataset)],
        "evolving": statements.evolving_statements(dataset),
    }


def lot_view(dataset: Dataset, lot_id: str) -> dict:
    """同一来源放回一致边界核对全链条，同时列出每份合同口径下的结果。"""
    lot = dataset.lot(lot_id)
    outputs = [o for o in dataset.outputs.values() if o.lot_id == lot_id]
    bases = []
    for boundary_id in sorted(dataset.boundaries):
        boundary = dataset.boundary(boundary_id)
        for as_of in _vintages(dataset):
            evidence = emissions.evidence_label(dataset, as_of)
            total = emissions.lot_total(dataset, lot_id, boundary, as_of)
            bases.append(
                {
                    "boundary_id": boundary_id,
                    "evidence": evidence,
                    "total_kg": emissions.round_range(total["total_kg"]),
                    "outputs": [
                        {
                            "output_id": part["output_id"],
                            "unit": part["unit"],
                            "quantity": part["quantity"],
                            "allocation_weight": part["allocation_weight"],
                            "per_unit_kg": emissions.round_range(part["per_unit_kg"]),
                            "total_kg": emissions.round_range(part["total_kg"]),
                        }
                        for part in total["outputs"]
                    ],
                    "basis": total["method"],
                }
            )
    comparisons = []
    for i, left in enumerate(bases):
        for right in bases[i + 1:]:
            a = dataset.boundary(left["boundary_id"])
            b = dataset.boundary(right["boundary_id"])
            verdict = emissions.comparability(a, b, left["evidence"], right["evidence"])
            comparisons.append(
                {
                    "left": {"boundary_id": left["boundary_id"], "evidence": left["evidence"]},
                    "right": {"boundary_id": right["boundary_id"], "evidence": right["evidence"]},
                    "comparable": verdict["comparable"],
                    "reasons": verdict["reasons"],
                }
            )
    return {
        "disclaimer": DISCLAIMER,
        "lot_id": lot_id,
        "assumption_group": lot.assumption_group,
        "feedstock": emissions.lot_inputs(dataset, lot_id),
        "baseline": _baseline_view(dataset, lot_id),
        "outputs": [
            {
                "output_id": o.output_id,
                "product": o.product,
                "unit": o.unit,
                "quantity": float(o.quantity),
                "substitute_factor": o.substitute_factor,
                "energy_share": o.energy_share,
                "value_share": o.value_share,
            }
            for o in outputs
        ],
        "by_boundary": bases,
        "comparisons": comparisons,
        "unallocated": _unallocated(dataset, lot_id),
    }


def _vintages(dataset: Dataset) -> list:
    """证据版本：初始登记，以及每次更正可知后的版本。"""
    import datetime

    stamps = sorted({c.available_at for c in dataset.corrections})
    if not stamps:
        return [None]
    first = datetime.datetime.fromisoformat(stamps[0])
    result = [(first - datetime.timedelta(microseconds=1)).isoformat()]
    result.extend(stamps)
    return result


def _baseline_view(dataset: Dataset, lot_id: str) -> dict:
    result = {}
    for as_of in _vintages(dataset):
        evidence = emissions.evidence_label(dataset, as_of)
        extremes = emissions.baseline_extremes(dataset, lot_id, as_of)
        result[evidence["vintage"]] = {
            "evidence": evidence,
            "gross_baseline_kg": {
                "min": round(extremes["gross_min"], 6),
                "max": round(extremes["gross_max"], 6),
            },
            "components": [
                {
                    "record_id": c.record_id,
                    "use": c.use,
                    "share_min": c.share_min,
                    "share_max": c.share_max,
                    "factor_min": c.factor_min,
                    "factor_max": c.factor_max,
                    "correction_ids": list(c.correction_ids),
                }
                for c in extremes["components"]
            ],
            "share_endpoints": {
                "min_case": extremes["gross_min_shares"],
                "max_case": extremes["gross_max_shares"],
                "note": "识别范围端点，不是对真实原用途的断言；下界配下界、上界配上界分别求解",
            },
        }
    return result


def _unallocated(dataset: Dataset, lot_id: str) -> list:
    result = []
    for output in dataset.outputs.values():
        if output.lot_id != lot_id:
            continue
        pos = ledger.position(dataset, output.output_id)
        result.append(pos.as_dict())
    return result


def statement_view(dataset: Dataset, claim_id: str) -> dict:
    return {
        "disclaimer": DISCLAIMER,
        "trace": statements.trace_statement(dataset, claim_id),
        "reassessment": [
            entry for entry in statements.evolving_statements(dataset) if entry["claim_id"] == claim_id
        ],
    }


def correction_view(dataset: Dataset, correction_id: str) -> dict:
    return {
        "disclaimer": DISCLAIMER,
        "trace": statements.trace_correction(dataset, correction_id),
    }


def summary(dataset: Dataset) -> dict:
    """全量核对：共享假设分组、可承诺量、并发请求与证据影响。"""
    grouped = {}
    for lot in dataset.feedstock.values():
        grouped.setdefault(lot.assumption_group, []).append(lot.lot_id)
    return {
        "disclaimer": DISCLAIMER,
        "assumption_groups": grouped,
        "correlation_note": (
            "同一 assumption_group 的原料共用运输、电网与生物源因子；"
            "各客户分别取最有利端点后相加，不等于联合可实现的总量。"
        ),
        "positions": {k: v.as_dict() for k, v in ledger.positions(dataset).items()},
        "overcommitments": ledger.overcommitments(dataset),
        "incoming": ledger.settle_incoming(dataset),
        "evidence": emissions.evidence_label(dataset),
        "evolving": statements.evolving_statements(dataset),
        "portfolio": portfolio.portfolio(dataset),
        "corrections": [
            {
                "correction_id": c.correction_id,
                "impact": statements.correction_impact(dataset, c.correction_id),
            }
            for c in dataset.corrections
        ],
    }
