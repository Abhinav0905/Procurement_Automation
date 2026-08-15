"""Stage 3 - historical purchasing tools.

This is what a buyer actually does before going to market: look up what the
company last paid, who supplied it, whether they delivered on time, whether the
price is drifting, and what a defensible target price looks like. Each of those
is a bounded query against the SAP mirror, assembled here into one benchmark
object that the RFQ, the negotiation and the award justification all reference.

Nothing in this module goes near a language model. Every number is arithmetic
over indexed rows, so a buyer can reproduce it in SQL and an auditor can
challenge it.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from procureguard.domain.enums import DecisionType
from procureguard.domain.units import UnitConverter
from procureguard.infrastructure.factory import ServiceContext
from procureguard.observability import logger

log = logger(__name__)

ZERO = Decimal(0)


@dataclass(slots=True)
class VendorHistory:
    vendor_id: str
    vendor_name: str
    order_count: int
    total_quantity: Decimal
    total_spend_base: Decimal
    weighted_avg_unit_price: Decimal | None
    min_unit_price: Decimal | None
    last_order_date: str
    on_time_pct: float | None = None
    rejection_pct: float | None = None
    defect_ppm: int | None = None
    avg_days_late: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "vendor_id": self.vendor_id,
            "vendor_name": self.vendor_name,
            "order_count": self.order_count,
            "total_quantity": _s(self.total_quantity),
            "total_spend_base": _s(self.total_spend_base),
            "weighted_avg_unit_price": _s(self.weighted_avg_unit_price),
            "min_unit_price": _s(self.min_unit_price),
            "last_order_date": self.last_order_date,
            "on_time_pct": self.on_time_pct,
            "rejection_pct": self.rejection_pct,
            "defect_ppm": self.defect_ppm,
            "avg_days_late": self.avg_days_late,
        }


@dataclass(slots=True)
class PriceBenchmark:
    """Everything needed to judge whether a quoted price is reasonable."""

    material_code: str
    base_currency: str
    base_uom: str
    requested_quantity: Decimal
    has_history: bool = False
    order_count: int = 0
    window_months: int = 36
    last_unit_price: Decimal | None = None
    last_order_date: str = ""
    last_vendor_id: str = ""
    min_unit_price: Decimal | None = None
    max_unit_price: Decimal | None = None
    median_unit_price: Decimal | None = None
    p25_unit_price: Decimal | None = None
    p75_unit_price: Decimal | None = None
    weighted_avg_unit_price: Decimal | None = None
    total_spend_base: Decimal = ZERO
    price_trend_pct_per_year: Decimal | None = None
    volatility_pct: Decimal | None = None
    quantity_adjusted_price: Decimal | None = None
    should_cost: Decimal | None = None
    should_cost_basis: str = ""
    target_price: Decimal | None = None
    active_info_record_price: Decimal | None = None
    active_info_record_vendor: str = ""
    contract_reference: str = ""
    standard_price: Decimal | None = None
    vendors: list[VendorHistory] = field(default_factory=list)
    monthly_trend: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)

    @property
    def benchmark_unit_price(self) -> Decimal | None:
        """The single number used for variance checks.

        Preference order reflects how defensible each source is: a live contract
        price beats a maintained info record, which beats the market's own
        recent behaviour, which beats a book value.
        """
        for candidate in (
            self.active_info_record_price,
            self.quantity_adjusted_price,
            self.median_unit_price,
            self.weighted_avg_unit_price,
            self.last_unit_price,
            self.standard_price,
        ):
            if candidate is not None and candidate > 0:
                return candidate
        return None

    def extended_benchmark(self) -> Decimal | None:
        price = self.benchmark_unit_price
        return price * self.requested_quantity if price is not None else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "material_code": self.material_code,
            "base_currency": self.base_currency,
            "base_uom": self.base_uom,
            "requested_quantity": _s(self.requested_quantity),
            "has_history": self.has_history,
            "order_count": self.order_count,
            "window_months": self.window_months,
            "last_unit_price": _s(self.last_unit_price),
            "last_order_date": self.last_order_date,
            "last_vendor_id": self.last_vendor_id,
            "min_unit_price": _s(self.min_unit_price),
            "max_unit_price": _s(self.max_unit_price),
            "median_unit_price": _s(self.median_unit_price),
            "p25_unit_price": _s(self.p25_unit_price),
            "p75_unit_price": _s(self.p75_unit_price),
            "weighted_avg_unit_price": _s(self.weighted_avg_unit_price),
            "quantity_adjusted_price": _s(self.quantity_adjusted_price),
            "benchmark_unit_price": _s(self.benchmark_unit_price),
            "should_cost": _s(self.should_cost),
            "should_cost_basis": self.should_cost_basis,
            "target_price": _s(self.target_price),
            "price_trend_pct_per_year": _s(self.price_trend_pct_per_year),
            "volatility_pct": _s(self.volatility_pct),
            "total_spend_base": _s(self.total_spend_base),
            "active_info_record_price": _s(self.active_info_record_price),
            "active_info_record_vendor": self.active_info_record_vendor,
            "contract_reference": self.contract_reference,
            "standard_price": _s(self.standard_price),
            "vendors": [v.to_dict() for v in self.vendors],
            "monthly_trend": self.monthly_trend,
            "notes": self.notes,
        }


@dataclass(slots=True)
class HistoricalContext:
    """Compatibility wrapper retained for callers that only need the essentials."""

    material_code: str
    purchases: tuple[dict[str, Any], ...]
    approved_suppliers: tuple[dict[str, Any], ...]
    benchmark: PriceBenchmark | None = None


class HistoricalProcurementService:
    """The buyer's research toolkit."""

    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx
        self.history = ctx.repos.history
        self.materials = ctx.repos.materials
        self.info_records = ctx.repos.info_records
        self.contracts = ctx.repos.contracts

    # ---------------------------------------------------------------- headline
    def build_benchmark(
        self,
        material_code: str,
        *,
        requested_quantity: Decimal,
        requested_uom: str = "",
        plant_code: str = "",
        window_months: int = 36,
        case_id: str = "",
    ) -> PriceBenchmark:
        settings = self.ctx.settings
        material = self.materials.get(material_code)
        base_uom = material.base_uom if material else (requested_uom or "EA")

        quantity = Decimal(str(requested_quantity))
        if requested_uom and material and requested_uom != base_uom:
            converter = UnitConverter(self.materials.get_alternate_units(material_code))
            if converter.can_convert(requested_uom, base_uom):
                quantity = converter.convert(quantity, requested_uom, base_uom)

        benchmark = PriceBenchmark(
            material_code=material_code,
            base_currency=settings.base_currency,
            base_uom=base_uom,
            requested_quantity=quantity,
            window_months=window_months,
        )

        stats = self.history.get_price_statistics(
            material_code,
            months=window_months,
            plant_code=plant_code,
            base_currency=settings.base_currency,
        )
        benchmark.order_count = int(stats.get("order_count", 0))
        benchmark.has_history = benchmark.order_count > 0

        if benchmark.has_history:
            benchmark.min_unit_price = stats.get("min_unit_price")
            benchmark.max_unit_price = stats.get("max_unit_price")
            benchmark.median_unit_price = stats.get("median_unit_price")
            benchmark.p25_unit_price = stats.get("p25_unit_price")
            benchmark.p75_unit_price = stats.get("p75_unit_price")
            benchmark.weighted_avg_unit_price = stats.get("weighted_avg_unit_price")
            benchmark.total_spend_base = stats.get("total_spend_base") or ZERO

            recent = self.history.get_last_purchases(material_code, 5, plant_code=plant_code)
            if recent:
                benchmark.last_unit_price = recent[0]["unit_price_base"]
                benchmark.last_order_date = recent[0]["order_date"]
                benchmark.last_vendor_id = recent[0]["vendor_id"]
                benchmark.evidence_ids = [row["evidence_id"] for row in recent]

            benchmark.monthly_trend = self.history.get_price_trend(
                material_code, months=min(window_months, 24), plant_code=plant_code
            )
            benchmark.price_trend_pct_per_year = _annualised_trend(benchmark.monthly_trend)
            benchmark.volatility_pct = _volatility(benchmark.monthly_trend)
            benchmark.vendors = self._vendor_histories(material_code, window_months)
            benchmark.quantity_adjusted_price = self._quantity_adjusted_price(
                material_code, quantity
            )
        else:
            benchmark.notes.append(
                f"No purchase history for {material_code} in the last {window_months} months; "
                f"this is a first-buy and the price cannot be benchmarked internally"
            )

        self._apply_reference_prices(benchmark, material_code, plant_code)
        self._compute_should_cost(benchmark)
        self._compute_target(benchmark)
        self._add_notes(benchmark)

        if case_id:
            self.ctx.repos.decisions.record(
                case_id=case_id,
                decision_type=DecisionType.HISTORICAL_BENCHMARK.value,
                recommendation=benchmark.to_dict(),
                rationale=(
                    f"Benchmark {benchmark.benchmark_unit_price} {settings.base_currency}/"
                    f"{base_uom} derived from {benchmark.order_count} historical order lines"
                    if benchmark.has_history
                    else "No internal price history available"
                ),
                confidence=_benchmark_confidence(benchmark),
                model_metadata={"engine": "deterministic-history-v1"},
                evidence=[
                    {
                        "evidence_type": "PURCHASE_HISTORY",
                        "evidence_id": evidence_id,
                        "role": "BASELINE",
                    }
                    for evidence_id in benchmark.evidence_ids[:10]
                ],
            )
        return benchmark

    # ---------------------------------------------------------------- helpers
    def _vendor_histories(self, material_code: str, window_months: int) -> list[VendorHistory]:
        out: list[VendorHistory] = []
        for row in self.history.get_vendors_for_material(
            material_code, months=window_months, limit=15
        ):
            performance = self.history.get_vendor_performance(row["vendor_id"], months=window_months)
            out.append(
                VendorHistory(
                    vendor_id=row["vendor_id"],
                    vendor_name=row["vendor_name"] or "",
                    order_count=row["order_count"],
                    total_quantity=row["total_quantity"] or ZERO,
                    total_spend_base=row["total_spend_base"] or ZERO,
                    weighted_avg_unit_price=row["weighted_avg_unit_price"],
                    min_unit_price=row["min_unit_price"],
                    last_order_date=row["last_order_date"],
                    on_time_pct=performance.get("on_time_pct"),
                    rejection_pct=performance.get("rejection_pct"),
                    defect_ppm=performance.get("defect_ppm"),
                    avg_days_late=row.get("avg_days_late", 0.0),
                )
            )
        return out

    def _quantity_adjusted_price(
        self, material_code: str, requested_quantity: Decimal
    ) -> Decimal | None:
        """Price at a comparable order size.

        Comparing a 5,000-piece RFQ against a history of 50-piece top-ups is the
        classic way to "prove" a supplier is expensive when they are not. This
        takes the median of historical lines within half-to-double the requested
        quantity, and falls back to a fitted curve when that band is empty.
        """
        curve = self.history.get_quantity_price_curve(material_code, months=60)
        if not curve or requested_quantity <= 0:
            return None

        low = requested_quantity / 2
        high = requested_quantity * 2
        in_band = [
            float(point["unit_price_base"])
            for point in curve
            if point["unit_price_base"] and low <= point["quantity"] <= high
        ]
        if len(in_band) >= 3:
            return Decimal(str(round(statistics.median(in_band), 6)))

        # Log-log regression of price against quantity: the standard
        # quantity-discount shape, and it degrades gracefully to a flat median.
        points = [
            (float(p["quantity"]), float(p["unit_price_base"]))
            for p in curve
            if p["quantity"] and p["unit_price_base"] and p["quantity"] > 0 and p["unit_price_base"] > 0
        ]
        if len(points) < 5:
            return None
        import math

        xs = [math.log(q) for q, _ in points]
        ys = [math.log(p) for _, p in points]
        mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
        denominator = sum((x - mean_x) ** 2 for x in xs)
        if denominator == 0:
            return None
        slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=False)) / denominator
        # Clamp the elasticity: real quantity discounts are meaningful but not
        # unbounded, and an outlier-driven slope produces absurd targets.
        slope = max(-0.45, min(0.05, slope))
        intercept = mean_y - slope * mean_x
        predicted = math.exp(intercept + slope * math.log(float(requested_quantity)))
        if not math.isfinite(predicted) or predicted <= 0:
            return None
        return Decimal(str(round(predicted, 6)))

    def _apply_reference_prices(
        self, benchmark: PriceBenchmark, material_code: str, plant_code: str
    ) -> None:
        records = self.info_records.list_for_material(material_code)
        if records:
            # Info records are held in the supplier's currency. Using that figure
            # directly as a base-currency benchmark silently compares yen to
            # dollars, so it is converted before it can influence a target price.
            cheapest = min(records, key=lambda r: self._to_base(r.net_price, r.currency, r.price_unit))
            native_price = Decimal(str(cheapest.net_price)) / max(int(cheapest.price_unit or 1), 1)
            benchmark.active_info_record_price = self._to_base(
                cheapest.net_price, cheapest.currency, cheapest.price_unit
            )
            benchmark.active_info_record_vendor = cheapest.vendor_id
            benchmark.notes.append(
                f"Active info record {cheapest.info_record_number} with {cheapest.vendor_id} at "
                f"{native_price} {cheapest.currency}/{cheapest.order_uom} "
                f"({benchmark.active_info_record_price} {benchmark.base_currency})"
            )

        contracts = self.contracts.find_for_material(material_code)
        if contracts:
            benchmark.contract_reference = contracts[0].contract_number
            benchmark.notes.append(
                f"Framework contract {contracts[0].contract_number} with "
                f"{contracts[0].vendor_id} covers this material and expires "
                f"{contracts[0].valid_to.date()}"
            )

        if plant_code:
            extension = self.materials.get_plant_extension(material_code, plant_code)
            if extension and extension.standard_price is not None:
                benchmark.standard_price = Decimal(str(extension.standard_price)) / max(
                    int(extension.price_unit or 1), 1
                )

    def _to_base(self, amount: Any, currency: str, price_unit: Any) -> Decimal:
        """Convert a master-data price into base currency, per single unit."""
        per_unit = Decimal(str(amount or 0)) / max(int(price_unit or 1), 1)
        base = self.ctx.settings.base_currency
        if not currency or currency == base:
            return per_unit
        rate = self.ctx.repos.fx.latest_rate(currency, base)
        if rate is None:
            inverse = self.ctx.repos.fx.latest_rate(base, currency)
            rate = (Decimal(1) / inverse) if inverse and inverse != 0 else None
        if rate is None:
            log.info("info_record_fx_missing", currency=currency, base=base)
            return per_unit
        return (per_unit * rate).quantize(Decimal("0.000001"))

    @staticmethod
    def _compute_should_cost(benchmark: PriceBenchmark) -> None:
        """A defensible floor to negotiate towards.

        Deliberately conservative: the best price the company has genuinely
        achieved, adjusted for quantity, is a fact - not a modelled cost
        breakdown that a supplier can argue with.
        """
        candidates: list[tuple[Decimal, str]] = []
        if benchmark.p25_unit_price is not None and benchmark.p25_unit_price > 0:
            candidates.append((benchmark.p25_unit_price, "25th percentile of historical prices"))
        if benchmark.min_unit_price is not None and benchmark.min_unit_price > 0:
            candidates.append((benchmark.min_unit_price, "best historical price achieved"))
        if benchmark.quantity_adjusted_price is not None:
            candidates.append(
                (benchmark.quantity_adjusted_price, "quantity-adjusted price curve")
            )
        if not candidates:
            return
        best, basis = min(candidates, key=lambda item: item[0])
        benchmark.should_cost = best
        benchmark.should_cost_basis = basis

    def _compute_target(self, benchmark: PriceBenchmark) -> None:
        reference = benchmark.benchmark_unit_price
        if reference is None:
            return
        target_pct = Decimal(str(self.ctx.settings.negotiation_target_savings_pct))
        target = reference * (Decimal(1) - target_pct / Decimal(100))
        # Never target below the demonstrated floor; that is how you get a
        # supplier to walk away or quietly substitute a cheaper part.
        if benchmark.should_cost is not None:
            target = max(target, benchmark.should_cost)
        benchmark.target_price = target.quantize(Decimal("0.000001"))

    @staticmethod
    def _add_notes(benchmark: PriceBenchmark) -> None:
        if benchmark.price_trend_pct_per_year is not None:
            trend = benchmark.price_trend_pct_per_year
            if trend >= 8:
                benchmark.notes.append(
                    f"Price is escalating at {trend}% per year; seek a fixed-price or "
                    f"index-linked agreement"
                )
            elif trend <= -8:
                benchmark.notes.append(
                    f"Price is falling at {abs(trend)}% per year; the market is softening in "
                    f"the buyer's favour"
                )
        if benchmark.volatility_pct is not None and benchmark.volatility_pct >= 25:
            benchmark.notes.append(
                f"Price volatility is {benchmark.volatility_pct}%; consider a longer validity "
                f"period or a price-adjustment clause"
            )
        if benchmark.order_count and len(benchmark.vendors) == 1:
            benchmark.notes.append(
                f"All {benchmark.order_count} historical orders went to "
                f"{benchmark.vendors[0].vendor_id}; there is no competitive reference price"
            )
        weak = [
            v
            for v in benchmark.vendors
            if v.on_time_pct is not None and v.on_time_pct < 85 and v.order_count >= 3
        ]
        for vendor in weak:
            benchmark.notes.append(
                f"Incumbent {vendor.vendor_id} delivered on time only {vendor.on_time_pct}% of "
                f"the time across {vendor.order_count} orders"
            )

    # ------------------------------------------------------- legacy interface
    def build_context(self, material_code: str, purchase_limit: int = 10) -> HistoricalContext:
        return HistoricalContext(
            material_code=material_code,
            purchases=tuple(self.history.get_last_purchases(material_code, purchase_limit)),
            approved_suppliers=tuple(self.history.get_approved_suppliers(material_code)),
        )

    def price_variance_flags(
        self, *, quoted_unit_price: Decimal, benchmark: PriceBenchmark
    ) -> tuple[str, ...]:
        return self.ctx.policy.price_variance_flags(
            quoted_unit_price=quoted_unit_price,
            benchmark_unit_price=benchmark.benchmark_unit_price,
        )


def _annualised_trend(monthly: Sequence[dict[str, Any]]) -> Decimal | None:
    """Least-squares slope of monthly price, expressed as percent per year."""
    points = [
        (index, float(row["weighted_avg_unit_price"]))
        for index, row in enumerate(monthly)
        if row.get("weighted_avg_unit_price")
    ]
    if len(points) < 4:
        return None
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0 or mean_y == 0:
        return None
    slope_per_month = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=False)) / denominator
    return Decimal(str(round((slope_per_month * 12 / mean_y) * 100, 2)))


def _volatility(monthly: Sequence[dict[str, Any]]) -> Decimal | None:
    prices = [
        float(row["weighted_avg_unit_price"])
        for row in monthly
        if row.get("weighted_avg_unit_price")
    ]
    if len(prices) < 3:
        return None
    mean = sum(prices) / len(prices)
    if mean == 0:
        return None
    return Decimal(str(round(statistics.pstdev(prices) / mean * 100, 2)))


def _benchmark_confidence(benchmark: PriceBenchmark) -> Decimal:
    if not benchmark.has_history:
        return Decimal("0.1")
    score = Decimal("0.4")
    if benchmark.order_count >= 10:
        score += Decimal("0.2")
    elif benchmark.order_count >= 3:
        score += Decimal("0.1")
    if len(benchmark.vendors) >= 2:
        score += Decimal("0.15")
    if benchmark.quantity_adjusted_price is not None:
        score += Decimal("0.15")
    if benchmark.volatility_pct is not None and benchmark.volatility_pct < 15:
        score += Decimal("0.1")
    return min(score, Decimal("0.95"))


def _s(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
