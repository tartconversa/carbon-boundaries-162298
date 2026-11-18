"""把资料包读成带类型的不可变业务记录。

只做读取、字段级校验和本地业务一致性校验；不计算指标。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sourcepack.catalog import read_table


class DataError(ValueError):
    """资料内容不满足本地业务约定。"""


@dataclass(frozen=True)
class Feedstock:
    lot_id: str
    wet_t: float
    moisture_pct: float
    processing_kwh: float
    transport_km: float
    assumption_group: str


@dataclass(frozen=True)
class BaselineOption:
    record_id: str
    lot_id: str
    use: str
    share_min: float
    share_max: float
    factor_min: float
    factor_max: float


@dataclass(frozen=True)
class Output:
    output_id: str
    lot_id: str
    product: str
    unit: str
    quantity: Decimal
    substitute_factor: float
    energy_share: float
    value_share: float


@dataclass(frozen=True)
class Boundary:
    boundary_id: str
    allocation: str
    include_processing: bool
    include_transport: bool
    include_biogenic: bool
    horizon_years: int


@dataclass(frozen=True)
class Factor:
    factor_id: str
    assumption_group: str
    kind: str
    min: float
    max: float


@dataclass(frozen=True)
class Claim:
    claim_id: str
    customer: str
    output_id: str
    boundary_id: str
    quantity: Decimal
    status: str
    at: str
    issued_at: str | None = None


@dataclass(frozen=True)
class IncomingRequest:
    request_id: str
    output_id: str
    quantity: Decimal
    at: str


@dataclass(frozen=True)
class Correction:
    correction_id: str
    record_id: str
    share_min: float
    share_max: float
    available_at: str


def _dec(value: str) -> Decimal:
    return Decimal(value)


@dataclass(frozen=True)
class Dataset:
    root: Path
    feedstock: dict
    baseline_options: tuple
    outputs: dict
    boundaries: dict
    factors: tuple
    claims: tuple
    incoming: tuple
    corrections: tuple

    def output(self, output_id: str) -> Output:
        try:
            return self.outputs[output_id]
        except KeyError:
            raise DataError("未知产出：" + output_id) from None

    def boundary(self, boundary_id: str) -> Boundary:
        try:
            return self.boundaries[boundary_id]
        except KeyError:
            raise DataError("未知边界：" + boundary_id) from None

    def lot(self, lot_id: str) -> Feedstock:
        try:
            return self.feedstock[lot_id]
        except KeyError:
            raise DataError("未知原料批：" + lot_id) from None

    def claim(self, claim_id: str) -> Claim:
        for claim in self.claims:
            if claim.claim_id == claim_id:
                return claim
        raise DataError("未知声明：" + claim_id)

    def baseline_for(self, lot_id: str) -> tuple:
        return tuple(r for r in self.baseline_options if r.lot_id == lot_id)

    def group_factors(self, group: str) -> dict:
        result = {}
        for factor in self.factors:
            if factor.assumption_group == group:
                result[factor.kind] = factor
        return result

    def output_ids(self) -> tuple:
        return tuple(self.outputs)


def _number(row, name):
    return float(row[name])


def _boolean(row, name):
    return row[name] == "true"


def load_dataset(root) -> Dataset:
    """读取资料并做本地业务一致性检查。"""
    root = Path(root).resolve()
    catalog = json.loads((root / "data/catalog.json").read_text(encoding="utf-8"))
    tables = {t["name"]: t for t in catalog["tables"]}
    rows = {name: read_table(root / table["path"], table) for name, table in tables.items()}

    feedstock = {}
    for row in rows["feedstock"]:
        item = Feedstock(
            lot_id=row["lot_id"],
            wet_t=_number(row, "wet_t"),
            moisture_pct=_number(row, "moisture_pct"),
            processing_kwh=_number(row, "processing_kwh"),
            transport_km=_number(row, "transport_km"),
            assumption_group=row["assumption_group"],
        )
        if not (0 <= item.moisture_pct < 100):
            raise DataError("水分越界：" + item.lot_id)
        if min(item.wet_t, item.processing_kwh, item.transport_km) < 0:
            raise DataError("原料投入为负：" + item.lot_id)
        feedstock[item.lot_id] = item

    baseline_options = tuple(
        BaselineOption(
            record_id=row["record_id"],
            lot_id=row["lot_id"],
            use=row["use"],
            share_min=_number(row, "share_min"),
            share_max=_number(row, "share_max"),
            factor_min=_number(row, "factor_min"),
            factor_max=_number(row, "factor_max"),
        )
        for row in rows["baseline_options"]
    )
    for option in baseline_options:
        if not (0 <= option.share_min <= option.share_max <= 1):
            raise DataError("原用途份额界限无效：" + option.record_id)
        if not (0 <= option.factor_min <= option.factor_max):
            raise DataError("原用途排放系数无效：" + option.record_id)
        if option.lot_id not in feedstock:
            raise DataError("原用途引用未知原料：" + option.record_id)

    outputs = {}
    for row in rows["outputs"]:
        item = Output(
            output_id=row["output_id"],
            lot_id=row["lot_id"],
            product=row["product"],
            unit=row["unit"],
            quantity=_dec(row["quantity"]),
            substitute_factor=_number(row, "substitute_factor"),
            energy_share=_number(row, "energy_share"),
            value_share=_number(row, "value_share"),
        )
        if item.lot_id not in feedstock:
            raise DataError("产出引用未知原料：" + item.output_id)
        if item.quantity <= 0:
            raise DataError("可分配产出必须为正：" + item.output_id)
        if min(item.energy_share, item.value_share) < 0:
            raise DataError("分配权重为负：" + item.output_id)
        outputs[item.output_id] = item

    boundaries = {}
    for row in rows["boundaries"]:
        item = Boundary(
            boundary_id=row["boundary_id"],
            allocation=row["allocation"],
            include_processing=_boolean(row, "include_processing"),
            include_transport=_boolean(row, "include_transport"),
            include_biogenic=_boolean(row, "include_biogenic"),
            horizon_years=int(row["horizon_years"]),
        )
        if item.allocation not in {"energy", "value", "system"}:
            raise DataError("未知分配约定：" + item.boundary_id)
        if item.horizon_years <= 0:
            raise DataError("比较年限必须为正：" + item.boundary_id)
        boundaries[item.boundary_id] = item

    factors = tuple(
        Factor(
            factor_id=row["factor_id"],
            assumption_group=row["assumption_group"],
            kind=row["kind"],
            min=_number(row, "min"),
            max=_number(row, "max"),
        )
        for row in rows["factors"]
    )
    for factor in factors:
        if factor.kind not in {"transport", "grid", "biogenic"}:
            raise DataError("未知因子类别：" + factor.factor_id)
        if not (0 <= factor.min <= factor.max):
            raise DataError("因子范围无效：" + factor.factor_id)

    claims = tuple(
        Claim(
            claim_id=row["claim_id"],
            customer=row["customer"],
            output_id=row["output_id"],
            boundary_id=row["boundary_id"],
            quantity=_dec(row["quantity"]),
            status=row["status"],
            at=row["at"],
            issued_at=None,
        )
        for row in rows["claim_requests"]
    )
    for claim in claims:
        if claim.status not in {"reserved", "issued"}:
            raise DataError("未知声明状态：" + claim.claim_id)
        if claim.quantity <= 0:
            raise DataError("占用量必须为正：" + claim.claim_id)
        if claim.output_id not in outputs:
            raise DataError("声明引用未知产出：" + claim.claim_id)
        if claim.boundary_id not in boundaries:
            raise DataError("声明引用未知边界：" + claim.claim_id)

    incoming = tuple(
        IncomingRequest(
            request_id=row["request_id"],
            output_id=row["output_id"],
            quantity=_dec(row["quantity"]),
            at=row["at"],
        )
        for row in rows["incoming"]
    )
    for request in incoming:
        if request.quantity <= 0:
            raise DataError("申请量必须为正：" + request.request_id)
        if request.output_id not in outputs:
            raise DataError("请求引用未知产出：" + request.request_id)

    corrections = tuple(
        Correction(
            correction_id=row["correction_id"],
            record_id=row["record_id"],
            share_min=_number(row, "share_min"),
            share_max=_number(row, "share_max"),
            available_at=row["available_at"],
        )
        for row in rows["corrections"]
    )
    by_record = {option.record_id for option in baseline_options}
    for correction in corrections:
        if correction.record_id not in by_record:
            raise DataError("更正引用未知原用途：" + correction.correction_id)
        if not (0 <= correction.share_min <= correction.share_max <= 1):
            raise DataError("更正份额界限无效：" + correction.correction_id)

    dataset = Dataset(
        root=root,
        feedstock=feedstock,
        baseline_options=baseline_options,
        outputs=outputs,
        boundaries=boundaries,
        factors=factors,
        claims=claims,
        incoming=incoming,
        corrections=corrections,
    )
    _check_semantics(dataset)
    return dataset


def _check_semantics(dataset: Dataset) -> None:
    errors = []
    for lot_id in dataset.feedstock:
        lot_outputs = [o for o in dataset.outputs.values() if o.lot_id == lot_id]
        if not lot_outputs:
            errors.append("原料没有产出：" + lot_id)
        options = dataset.baseline_for(lot_id)
        if not options:
            errors.append("原料没有原用途登记：" + lot_id)
        if abs(sum(o.energy_share for o in lot_outputs) - 1) > 1e-9:
            errors.append("能量分配权重在同一批内不归一：" + lot_id)
        if abs(sum(o.value_share for o in lot_outputs) - 1) > 1e-9:
            errors.append("价值分配权重在同一批内不归一：" + lot_id)
        if options:
            lower = sum(o.share_min for o in options)
            upper = sum(o.share_max for o in options)
            if lower > 1 + 1e-9 or upper < 1 - 1e-9:
                errors.append("原用途份额界限无法同时成立：" + lot_id)
    if errors:
        raise DataError("; ".join(errors))
