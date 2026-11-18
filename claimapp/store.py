"""可持久化的份额账本：预留、签发、释放与重新核对。

账本只记录业务动作，不改写资料包。动作按时间顺序校验；已签发的说明不能
被删除或改数，只能追加撤回或补充类型的新动作。
"""
from __future__ import annotations

import datetime
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from . import emissions, ledger
from .records import Claim, DataError, Dataset, IncomingRequest


def _stamp(value: str) -> datetime.datetime:
    parsed = datetime.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise DataError("时刻必须带时区：" + value)
    return parsed


def _dec(value) -> Decimal:
    return Decimal(str(value))


class LedgerStore:
    """JSON 账本；与基础资料合并后得到可核对的视图。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.state = {"claims": [], "incoming": [], "events": []}
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            self.state.setdefault("claims", [])
            self.state.setdefault("incoming", [])
            self.state.setdefault("events", [])

    # -- 视图 -------------------------------------------------------------
    def dataset(self, base: Dataset, as_of: str | None = None) -> Dataset:
        limit = _stamp(as_of) if as_of is not None else None
        stored_claims = []
        for row in self.state["claims"]:
            if limit is not None and _stamp(row["at"]) > limit:
                continue
            stored_claims.append(
                Claim(
                    claim_id=row["claim_id"],
                    customer=row["customer"],
                    output_id=row["output_id"],
                    boundary_id=row["boundary_id"],
                    quantity=_dec(row["quantity"]),
                    status=row["status"],
                    at=row["at"],
                    issued_at=row.get("issued_at"),
                )
            )
        stored_incoming = []
        for row in self.state["incoming"]:
            if limit is not None and _stamp(row["at"]) > limit:
                continue
            stored_incoming.append(
                IncomingRequest(
                    request_id=row["request_id"],
                    output_id=row["output_id"],
                    quantity=_dec(row["quantity"]),
                    at=row["at"],
                )
            )
        # 同一声明可被 release 缩减；账本视图以最终数量为准。
        return replace(
            base,
            claims=tuple(base.claims) + tuple(stored_claims),
            incoming=tuple(base.incoming) + tuple(stored_incoming),
        )

    # -- 动作 -------------------------------------------------------------
    def reserve(self, claim_id, customer, output_id, boundary_id, quantity, at, dataset) -> dict:
        _stamp(at)
        if any(row["claim_id"] == claim_id for row in self.state["claims"]) or any(
            claim.claim_id == claim_id for claim in dataset.claims
        ):
            raise DataError("声明编号已存在：" + claim_id)
        claim = Claim(
            claim_id=claim_id,
            customer=customer,
            output_id=output_id,
            boundary_id=boundary_id,
            quantity=_dec(quantity),
            status="reserved",
            at=at,
            issued_at=None,
        )
        if claim.quantity <= 0:
            raise DataError("预留量必须为正")
        if output_id not in dataset.outputs:
            raise DataError("未知产出：" + output_id)
        if boundary_id not in dataset.boundaries:
            raise DataError("未知边界：" + boundary_id)
        view = self.dataset(dataset)
        problems = ledger.check_claim(view, claim)
        if problems:
            raise DataError("预留超出可承诺量：" + json.dumps(problems, ensure_ascii=False))
        self.state["claims"].append(
            {
                "claim_id": claim_id,
                "customer": customer,
                "output_id": output_id,
                "boundary_id": boundary_id,
                "quantity": str(claim.quantity),
                "status": "reserved",
                "at": at,
            }
        )
        self.state["events"].append({"event": "reserve", "claim_id": claim_id, "at": at})
        self._save()
        return {"action": "reserve", "claim": claim_id, "position": ledger.position(self.dataset(dataset), output_id).as_dict()}

    def issue(self, claim_id, at, dataset) -> dict:
        _stamp(at)
        row = self._row(claim_id)
        if row["status"] != "reserved":
            raise DataError("只有预留可以签发：" + claim_id)
        if _stamp(at) < _stamp(row["at"]):
            raise DataError("签发时刻早于预留时刻")
        view = self.dataset(dataset)
        claim = Claim(
            claim_id=row["claim_id"],
            customer=row["customer"],
            output_id=row["output_id"],
            boundary_id=row["boundary_id"],
            quantity=_dec(row["quantity"]),
            status="issued",
            at=row["at"],
            issued_at=at,
        )
        problems = ledger.check_claim(view, claim, exclude=(claim_id,))
        if problems:
            raise DataError("签发将造成超额承诺：" + json.dumps(problems, ensure_ascii=False))
        row["status"] = "issued"
        row["issued_at"] = at
        issued_view = replace(
            view,
            claims=tuple(
                item if item.claim_id != claim_id else replace(item, status="issued", issued_at=at)
                for item in view.claims
            ),
        )
        self.state["events"].append({"event": "issue", "claim_id": claim_id, "at": at})
        self._save()
        return {
            "action": "issue",
            "claim": claim_id,
            "statement": _statement_for(issued_view, claim_id),
            "position": ledger.position(issued_view, row["output_id"]).as_dict(),
        }

    def release(self, claim_id, quantity, at, dataset) -> dict:
        _stamp(at)
        row = self._row(claim_id)
        if row["status"] == "cancelled":
            raise DataError("声明 %s 已全部释放，无未签发份额" % claim_id)
        if row["status"] != "reserved":
            raise DataError(
                "声明 %s 已签发，不能释放或改写；请按更正流程出具撤回或补充说明" % claim_id
            )
        amount = _dec(quantity)
        if amount <= 0:
            raise DataError("释放量必须为正")
        held = _dec(row["quantity"])
        if amount > held:
            raise DataError("释放量超过该预留的未签发数量 %s" % held)
        view = self.dataset(dataset)
        before = ledger.position(view, row["output_id"]).as_dict()
        row["quantity"] = str(held - amount)
        if _dec(row["quantity"]) == 0:
            row["status"] = "cancelled"
        self.state["events"].append(
            {"event": "release", "claim_id": claim_id, "quantity": str(amount), "at": at}
        )
        self._save()
        after = ledger.position(self.dataset(dataset), row["output_id"]).as_dict()
        return {
            "action": "release_reservation",
            "claim": claim_id,
            "released": str(amount),
            "remaining": row["quantity"],
            "status": row["status"],
            "before": before,
            "after": after,
            "issued_untouched": True,
        }

    # -- 内部 -------------------------------------------------------------
    def _row(self, claim_id: str) -> dict:
        for row in self.state["claims"]:
            if row["claim_id"] == claim_id:
                return row
        raise DataError("账本中没有该声明：" + claim_id)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    def as_dict(self) -> dict:
        return self.state


def _statement_for(dataset: Dataset, claim_id: str) -> dict:
    from . import statements

    claim = dataset.claim(claim_id)
    return statements.statement_at(dataset, claim, kind="issued").as_dict()
