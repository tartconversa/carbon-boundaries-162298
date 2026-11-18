"""声明与更正：把份额、边界口径和证据版本固定在一份可追溯的说明里。

已签发的说明是历史事实。新证据不改写它：影响被计算成撤回、替代或补充说明，
并列明差额和受影响的对象。
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass

from . import emissions
from .ledger import positions, settle_incoming
from .records import DataError, Dataset

STATEMENT_KINDS = ("issued", "reserved", "withdraw", "supersede", "supplement")
EVENT_KINDS = ("grant", "issue", "release")


@dataclass(frozen=True)
class Statement:
    statement_id: str
    claim_id: str
    customer: str
    output_id: str
    boundary_id: str
    kind: str
    quantity: str
    at: str
    evidence: dict
    calculation: dict
    share_basis: dict
    supersedes: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "statement_id": self.statement_id,
            "claim_id": self.claim_id,
            "customer": self.customer,
            "output_id": self.output_id,
            "boundary_id": self.boundary_id,
            "kind": self.kind,
            "quantity": self.quantity,
            "at": self.at,
            "evidence": self.evidence,
            "calculation": self.calculation,
            "share_basis": self.share_basis,
            "supersedes": self.supersedes,
            "reason": self.reason,
        }


def _stamp(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value)


def evidence_at(claim) -> str:
    """一份说明固定使用的证据时刻：已按签发时刻，否则按请求时刻。"""
    return claim.issued_at or claim.at


def statement_at(dataset: Dataset, claim, kind: str | None = None) -> Statement:
    """按声明可知时刻的证据版本计算一份声明。"""
    at = evidence_at(claim)
    boundary = dataset.boundary(claim.boundary_id)
    evidence = emissions.evidence_label(dataset, at)
    calculation = emissions.output_range(dataset, claim.output_id, boundary, at)
    share_basis = {
        "evidence": evidence,
        "components": [
            {
                "record_id": c.record_id,
                "use": c.use,
                "share_min": c.share_min,
                "share_max": c.share_max,
                "factor_min": c.factor_min,
                "factor_max": c.factor_max,
            }
            for c in emissions.lot_components(dataset, calculation["lot_id"], at)
        ],
        "gross_baseline_kg": {
            "min": calculation["baseline_kg"]["min"],
            "max": calculation["baseline_kg"]["max"],
        },
        "direct_kg": calculation["direct_kg"],
    }
    quantity = float(claim.quantity)
    return Statement(
        statement_id=f"{claim.claim_id}@{evidence['vintage']}",
        claim_id=claim.claim_id,
        customer=claim.customer,
        output_id=claim.output_id,
        boundary_id=claim.boundary_id,
        kind=kind or claim.status,
        quantity=str(claim.quantity),
        at=at,
        evidence=evidence,
        calculation={
            "unit": calculation["unit"],
            "allocation_weight": calculation["allocation_weight"],
            "per_unit_kg": emissions.round_range(calculation["per_unit_kg"]),
            "total_kg": emissions.round_range(
                {k: v * quantity for k, v in calculation["per_unit_kg"].items()}
            ),
            "basis": calculation["basis"],
        },
        share_basis=share_basis,
        reason=None,
    )


def statements(dataset: Dataset) -> list:
    """按声明发生时刻逐一出具说明；每条都固定当时的证据版本。"""
    return [statement_at(dataset, claim) for claim in dataset.claims]


def statement_for(dataset: Dataset, claim_id: str) -> Statement:
    for statement in statements(dataset):
        if statement.claim_id == claim_id:
            return statement
    raise DataError("未知声明：" + claim_id)


def evolving_statements(dataset: Dataset, as_of: str | None = None) -> list:
    """按当前证据判断每条历史声明是否仍可维持。

    未签发预留：按当前证据重评；已签发说明：不修改，必要时给出补充说明，
    列出与当时依据相比的差额。
    """
    current = emissions.evidence_label(dataset, as_of)
    result = []
    for claim in dataset.claims:
        original = statement_at(dataset, claim)
        boundary = dataset.boundary(claim.boundary_id)
        now = emissions.output_range(dataset, claim.output_id, boundary, as_of)
        quantity = float(claim.quantity)
        now_total = {k: v * quantity for k, v in now["per_unit_kg"].items()}
        delta = {
            "min": now_total["min"] - original.calculation["total_kg"]["min"],
            "max": now_total["max"] - original.calculation["total_kg"]["max"],
        }
        overlaps = (
            now_total["max"] >= original.calculation["total_kg"]["min"]
            and now_total["min"] <= original.calculation["total_kg"]["max"]
        )
        status = "unchanged" if current["vintage"] == original.evidence["vintage"] else (
            "supported" if overlaps else "affected"
        )
        entry = {
            "claim_id": claim.claim_id,
            "customer": claim.customer,
            "status": claim.status,
            "issued_at": claim.issued_at or claim.at,
            "original_statement": original.statement_id,
            "original_total_kg": original.calculation["total_kg"],
            "current_total_kg": emissions.round_range(now_total),
            "current_evidence": current,
            "delta_kg": emissions.round_range(delta),
            "range_overlap": overlaps,
            "assessment": status,
        }
        if claim.status == "issued" and status == "affected":
            entry["required_action"] = "supplement"
            entry["note"] = "已签发说明保持原数字与当时依据；请出具补充说明列明差额"
        elif claim.status == "issued" and status == "unchanged":
            entry["required_action"] = None
            entry["note"] = "证据版本未变，原说明继续有效"
        elif claim.status == "issued":
            entry["required_action"] = None
            entry["note"] = "新证据下仍被原区间覆盖；原说明保留，可追加提示"
        else:
            entry["required_action"] = "reassess_reservation"
            entry["note"] = "未签发预留按当前证据重评后可调整或释放"
        result.append(entry)
    return result


def correction_impact(dataset: Dataset, correction_id: str) -> dict:
    """一次来源更正影响了哪些客户、哪些声明和哪些可承诺量。"""
    correction = None
    for item in dataset.corrections:
        if item.correction_id == correction_id:
            correction = item
    if correction is None:
        raise DataError("未知更正：" + correction_id)
    option = None
    for item in dataset.baseline_options:
        if item.record_id == correction.record_id:
            option = item
    if option is None:
        raise DataError("更正引用的原用途不存在：" + correction.record_id)
    lot_id = option.lot_id
    before = emissions.evidence_label(dataset, None)
    # 更正生效前的证据版本：同一批更正中 available_at 相同的记录一并排除。
    earlier = _stamp(correction.available_at)
    prior_as_of = (earlier - datetime.timedelta(microseconds=1)).isoformat()
    prior_evidence = emissions.evidence_label(dataset, prior_as_of)

    claims = [c for c in dataset.claims if dataset.output(c.output_id).lot_id == lot_id]
    affected = []
    for claim in claims:
        boundary = dataset.boundary(claim.boundary_id)
        old = emissions.output_range(dataset, claim.output_id, boundary, prior_as_of)
        new = emissions.output_range(dataset, claim.output_id, boundary)
        q = float(claim.quantity)
        old_total = {k: v * q for k, v in old["per_unit_kg"].items()}
        new_total = {k: v * q for k, v in new["per_unit_kg"].items()}
        old_statement = statement_at(dataset, claim)
        action = _action_for(claim, old_statement, old_total, new_total)
        affected.append(
            {
                "claim_id": claim.claim_id,
                "customer": claim.customer,
                "status": claim.status,
                "output_id": claim.output_id,
                "boundary_id": claim.boundary_id,
                "quantity": str(claim.quantity),
                "original_total_kg": old_statement.calculation["total_kg"],
                "before_correction_total_kg": emissions.round_range(old_total),
                "after_correction_total_kg": emissions.round_range(new_total),
                "delta_kg": emissions.round_range(
                    {"min": new_total["min"] - old_total["min"], "max": new_total["max"] - old_total["max"]}
                ),
                "action": action,
            }
        )

    outputs = [o for o in dataset.outputs.values() if o.lot_id == lot_id]
    positions_before = positions(dataset)
    reservations = []
    for claim in claims:
        if claim.status != "reserved":
            continue
        boundary = dataset.boundary(claim.boundary_id)
        old = emissions.output_range(dataset, claim.output_id, boundary, prior_as_of)
        new = emissions.output_range(dataset, claim.output_id, boundary)
        reservations.append(
            {
                "claim_id": claim.claim_id,
                "customer": claim.customer,
                "before_per_unit_kg": emissions.round_range(old["per_unit_kg"]),
                "after_per_unit_kg": emissions.round_range(new["per_unit_kg"]),
                "action": "reassess",
            }
        )

    boundary = dataset.boundary(sorted(dataset.boundaries)[0])
    by_output = {}
    for output in outputs:
        before_pos = positions_before[output.output_id]
        before_ev = emissions.output_range(dataset, output.output_id, boundary, prior_as_of)
        current = emissions.output_range(dataset, output.output_id, boundary)
        by_output[output.output_id] = {
            "available_quantity": str(before_pos.available),
            "before_per_unit_kg": emissions.round_range(before_ev["per_unit_kg"]),
            "after_per_unit_kg": emissions.round_range(current["per_unit_kg"]),
            "available_claim_capacity_change": (
                "可承诺量按当前占用量不变；每单位减排区间已按更正后的份额界限重算"
            ),
        }

    return {
        "correction_id": correction.correction_id,
        "record_id": correction.record_id,
        "lot_id": lot_id,
        "new_bounds": {"share_min": correction.share_min, "share_max": correction.share_max},
        "available_at": correction.available_at,
        "evidence_before": prior_evidence,
        "evidence_after": before,
        "affected_claims": affected,
        "reservations_to_reassess": reservations,
        "outputs": by_output,
        "incoming_after_correction": settle_incoming(dataset),
    }


def _action_for(claim, original_statement, old_total, new_total) -> dict:
    issued = claim.status == "issued"
    same = old_total == new_total
    if same:
        return {"kind": "none", "note": "更正前后的区间相同，无需动作"}
    if not issued:
        return {"kind": "reassess", "note": "预留按更正后的区间重评"}
    old_low = original_statement.calculation["total_kg"]["min"]
    old_high = original_statement.calculation["total_kg"]["max"]
    if new_total["max"] < old_low or new_total["min"] > old_high:
        return {
            "kind": "withdraw_or_supersede",
            "note": "更正后区间与原签发区间不再相交；需撤回或出具替代说明",
        }
    return {
        "kind": "supplement",
        "note": "更正后区间与原签发区间部分重叠；原说明保留，出具补充说明列明差额",
    }


def trace_statement(dataset: Dataset, claim_id: str) -> dict:
    """从一份声明追到原料、产出、边界、共享假设与全部相关更正。"""
    claim = dataset.claim(claim_id)
    statement = statement_at(dataset, claim)
    output = dataset.output(claim.output_id)
    lot = dataset.lot(output.lot_id)
    boundary = dataset.boundary(claim.boundary_id)
    inputs = emissions.lot_inputs(dataset, lot.lot_id)
    rules = _rules(boundary, statement.calculation["allocation_weight"])
    return {
        "statement": statement.as_dict(),
        "claim": {
            "claim_id": claim.claim_id,
            "customer": claim.customer,
            "status": claim.status,
            "at": claim.at,
        },
        "output": {
            "output_id": output.output_id,
            "product": output.product,
            "unit": output.unit,
            "quantity": float(output.quantity),
            "substitute_factor": output.substitute_factor,
            "energy_share": output.energy_share,
            "value_share": output.value_share,
        },
        "feedstock": inputs,
        "boundary": boundary.__dict__,
        "rules": rules,
        "related_corrections": [
            {
                "correction_id": c.correction_id,
                "record_id": c.record_id,
                "share_min": c.share_min,
                "share_max": c.share_max,
                "available_at": c.available_at,
            }
            for c in dataset.corrections
            if c.record_id in {item.record_id for item in dataset.baseline_for(lot.lot_id)}
        ],
        "affects_other_claims": [
            other.claim_id
            for other in dataset.claims
            if other.claim_id != claim.claim_id
            and dataset.output(other.output_id).lot_id == lot.lot_id
        ],
    }


def _rules(boundary, weight) -> dict:
    return {
        "allocation": boundary.allocation,
        "weight": weight,
        "include_processing": boundary.include_processing,
        "include_transport": boundary.include_transport,
        "include_biogenic": boundary.include_biogenic,
        "horizon_years": boundary.horizon_years,
        "shared_group_note": "同一 assumption_group 的因子为共同参数，不可由客户各自取端",
    }


def trace_correction(dataset: Dataset, correction_id: str) -> dict:
    impact = correction_impact(dataset, correction_id)
    customers = sorted({item["customer"] for item in impact["affected_claims"]})
    statements_affected = [
        item["claim_id"] for item in impact["affected_claims"] if item["action"]["kind"] != "none"
    ]
    return {
        "correction_id": correction_id,
        "record_id": impact["record_id"],
        "lot_id": impact["lot_id"],
        "customers": customers,
        "claims_affected": statements_affected,
        "impact": impact,
    }
