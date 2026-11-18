"""份额分配：预留、签发、退出释放与并发请求。

守恒以真实产出数量为上限。同一产出的预留与已签发量合计不得超过可分配量；
竞争同一剩余量的并发请求按“最大可满足均等份额”一次性判定，不预设某一请求
优先，也不会共同形成超额承诺。
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal

from .records import DataError, Dataset

OPEN_STATUSES = ("reserved", "issued")


@dataclass(frozen=True)
class Position:
    output_id: str
    unit: str
    capacity: Decimal
    reserved: Decimal
    issued: Decimal

    @property
    def available(self) -> Decimal:
        return self.capacity - self.reserved - self.issued

    def as_dict(self) -> dict:
        return {
            "output_id": self.output_id,
            "unit": self.unit,
            "capacity": str(self.capacity),
            "reserved": str(self.reserved),
            "issued": str(self.issued),
            "available": str(self.available),
        }


def _stamp(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value)


def positions(dataset: Dataset, as_of: str | None = None) -> dict:
    """每个产出的占用情况；as_of 只计入该时刻及之前发生的声明。"""
    result = {}
    for output_id, output in dataset.outputs.items():
        result[output_id] = Position(
            output_id=output_id,
            unit=output.unit,
            capacity=output.quantity,
            reserved=Decimal(0),
            issued=Decimal(0),
        )
    limit = _stamp(as_of) if as_of is not None else None
    for claim in dataset.claims:
        if limit is not None and _stamp(claim.at) > limit:
            continue
        current = result[claim.output_id]
        if claim.status == "reserved":
            result[claim.output_id] = Position(
                current.output_id,
                current.unit,
                current.capacity,
                current.reserved + claim.quantity,
                current.issued,
            )
        elif claim.status == "issued":
            result[claim.output_id] = Position(
                current.output_id,
                current.unit,
                current.capacity,
                current.reserved,
                current.issued + claim.quantity,
            )
    return result


def position(dataset: Dataset, output_id: str, as_of: str | None = None) -> Position:
    try:
        return positions(dataset, as_of)[output_id]
    except KeyError:
        raise DataError("未知产出：" + output_id) from None


def overcommitments(dataset: Dataset) -> list:
    errors = []
    for output_id, pos in positions(dataset).items():
        if pos.reserved + pos.issued > pos.capacity:
            errors.append(
                {
                    "output_id": output_id,
                    "capacity": str(pos.capacity),
                    "reserved": str(pos.reserved),
                    "issued": str(pos.issued),
                    "excess": str(pos.reserved + pos.issued - pos.capacity),
                }
            )
    return errors


def check_claim(dataset: Dataset, claim, exclude=(), as_of: str | None = None) -> list:
    """检查一条预留/签发能否成立；返回未满足守恒时的说明。"""
    pos = position(dataset, claim.output_id, as_of)
    used = claim.quantity
    for other in dataset.claims:
        if other.claim_id in exclude or other.claim_id == claim.claim_id:
            continue
        if other.output_id != claim.output_id:
            continue
        if as_of is not None and _stamp(other.at) > _stamp(as_of):
            continue
        used += other.quantity if other.status in OPEN_STATUSES else Decimal(0)
    excess = used - pos.capacity
    if excess > 0:
        return [
            {
                "code": "overcommitment",
                "output_id": claim.output_id,
                "unit": pos.unit,
                "capacity": str(pos.capacity),
                "committed": str(used),
                "excess": str(excess),
            }
        ]
    return []


def release(dataset: Dataset, claim_id: str, quantity, as_of: str | None = None) -> dict:
    """客户缩减用量：只释放尚未签发的预留，已签发的份额保持不变。"""
    claim = dataset.claim(claim_id)
    amount = Decimal(str(quantity))
    if amount <= 0:
        raise DataError("释放量必须为正")
    if claim.status == "cancelled":
        raise DataError(f"声明 {claim_id} 已全部释放，无未签发份额")
    if claim.status != "reserved":
        raise DataError(
            f"声明 {claim_id} 已签发，不能释放或改写；请按更正流程出具撤回或补充说明"
        )
    if amount > claim.quantity:
        raise DataError(f"释放量超过该预留的未签发数量 {claim.quantity}")
    pos = position(dataset, claim.output_id, as_of)
    return {
        "action": "release_reservation",
        "claim_id": claim.claim_id,
        "output_id": claim.output_id,
        "unit": pos.unit,
        "released": str(amount),
        "remaining_reserved": str(claim.quantity - amount),
        "issued_untouched": str(pos.issued),
        "available_after": str(pos.available + amount),
        "customer": claim.customer,
    }


def settle_incoming(dataset: Dataset, as_of: str | None = None) -> dict:
    """对同时到达的竞争请求给出可执行判定。

    同一时刻到达的请求视为一个并发批次；批次可用的剩余量按批次内请求
    均等封顶（max-min fair），不假设任何请求优先。晚于批次的请求在批次
    结论之后另行判断。
    """
    limit = _stamp(as_of) if as_of is not None else None
    pool = {}
    for request in dataset.incoming:
        if limit is not None and _stamp(request.at) > limit:
            continue
        pool.setdefault((request.output_id, request.at), []).append(request)

    pos = positions(dataset, as_of)
    committed = {output_id: p.reserved + p.issued for output_id, p in pos.items()}
    claimed_events = {
        (_stamp(c.at), c.output_id, c.quantity) for c in dataset.claims if c.status in OPEN_STATUSES
    }
    results = []
    for (output_id, at), batch in sorted(pool.items(), key=lambda item: (item[0][1], item[0][0])):
        capacity = pos[output_id].capacity
        remaining = capacity - committed.get(output_id, Decimal(0))
        # 已作为声明登记的事件不再重复授出。
        batch = [r for r in batch if (_stamp(r.at), output_id, r.quantity) not in claimed_events]
        if remaining < 0:
            remaining = Decimal(0)
        demands = sorted((r.quantity, r.request_id) for r in batch)
        allocation = _max_min(demands, remaining)
        allocated_total = sum(allocation.values(), Decimal(0))
        for request in sorted(batch, key=lambda r: r.request_id):
            granted = allocation[request.request_id]
            results.append(
                {
                    "request_id": request.request_id,
                    "output_id": output_id,
                    "unit": pos[output_id].unit,
                    "requested": str(request.quantity),
                    "at": at,
                    "granted": str(granted),
                    "unmet": str(request.quantity - granted),
                    "rule": "concurrent batch: equal cap, no priority",
                    "batch_remaining_before": str(remaining),
                    "batch_granted": str(allocated_total),
                }
            )
        committed[output_id] = committed.get(output_id, Decimal(0)) + allocated_total
        if allocated_total > remaining:
            raise DataError("并发分配超出剩余量")

    return {
        "as_of": as_of,
        "requests": results,
        "remaining_by_output": {
            output_id: str(pos[output_id].capacity - committed[output_id])
            for output_id in pos
        },
    }


def _max_min(demands, remaining: Decimal) -> dict:
    """在总剩余量内对一组请求做等额封顶分配，余数按请求编号稳定落位。"""
    ordered = sorted(demands, key=lambda pair: (pair[0], pair[1]))
    allocation = {request_id: Decimal(0) for _, request_id in ordered}
    left = remaining
    active = [pair for pair in ordered if pair[0] > 0]
    while active and left > 0:
        cap = left / len(active)
        low = [pair for pair in active if pair[0] <= cap]
        if not low:
            for demand, request_id in active[:-1]:
                allocation[request_id] = cap
                left -= cap
            allocation[active[-1][1]] = left
            left = Decimal(0)
            break
        for demand, request_id in low:
            allocation[request_id] = demand
            left -= demand
        active = [pair for pair in active if pair[0] > cap]
    return allocation
