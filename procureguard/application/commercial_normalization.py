"""Stage 12 - commercial normalisation.

Three suppliers quote 142.50 EUR per piece EXW Milan, 168 USD per piece DAP
Detroit, and 15,200 CNY per 100 pieces FOB Shanghai. None of those numbers can
be compared until they are on one basis. This stage puts them there:

    quoted price
      -> price basis      (per 100 -> per 1)
      -> unit of measure  (per box -> per piece, via material master factors)
      -> currency         (at the rate on the date the quote was received)
      -> Incoterm         (add the cost elements the seller did not include)
      -> payment terms    (discount to present value at the company's WACC)
      -> lead time        (carrying cost of the delivery gap)
      -> quality risk     (expected cost of the supplier's defect rate)
      = total cost of ownership

Every adjustment is itemised and stored. A buyer who disagrees with the freight
assumption can see it, change it, and re-rank - which is the difference between
a decision-support tool and a black box.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from procureguard.domain.enums import DecisionType, Incoterm
from procureguard.domain.money import FxRateTable, Money, PaymentTerms
from procureguard.domain.units import UnitConverter
from procureguard.infrastructure.factory import ServiceContext
from procureguard.observability import logger

log = logger(__name__)

ZERO = Decimal(0)

# Which cost elements the BUYER must add for each Incoterm to reach a
# delivered-at-plant, duty-paid basis. Anything not listed is already in the
# seller's price.
INCOTERM_BUYER_COSTS: dict[str, frozenset[str]] = {
    Incoterm.EXW.value: frozenset({"origin_handling", "main_freight", "insurance", "customs", "duty", "dest_delivery"}),
    Incoterm.FCA.value: frozenset({"main_freight", "insurance", "customs", "duty", "dest_delivery"}),
    Incoterm.FAS.value: frozenset({"main_freight", "insurance", "customs", "duty", "dest_delivery"}),
    Incoterm.FOB.value: frozenset({"main_freight", "insurance", "customs", "duty", "dest_delivery"}),
    Incoterm.CFR.value: frozenset({"insurance", "customs", "duty", "dest_delivery"}),
    Incoterm.CPT.value: frozenset({"insurance", "customs", "duty", "dest_delivery"}),
    Incoterm.CIF.value: frozenset({"customs", "duty", "dest_delivery"}),
    Incoterm.CIP.value: frozenset({"customs", "duty", "dest_delivery"}),
    Incoterm.DAP.value: frozenset({"customs", "duty"}),
    Incoterm.DPU.value: frozenset({"customs", "duty"}),
    Incoterm.DDP.value: frozenset(),
}

# How a lane's total freight splits across the journey.
FREIGHT_SPLIT = {
    "origin_handling": Decimal("0.10"),
    "main_freight": Decimal("0.75"),
    "dest_delivery": Decimal("0.15"),
}


@dataclass(slots=True)
class Adjustment:
    code: str
    label: str
    amount_base: Decimal
    basis: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "label": self.label,
            "amount_base": str(self.amount_base),
            "basis": self.basis,
        }


@dataclass(slots=True)
class NormalizedLine:
    quotation_id: str
    quotation_line_id: str
    vendor_id: str
    rfq_line_number: int
    quantity_base_uom: Decimal
    base_uom: str
    quoted_unit_price: Decimal
    quoted_currency: str
    fx_rate: Decimal
    fx_as_of: date | None
    unit_price_base: Decimal
    ext_price_base: Decimal
    freight_base: Decimal = ZERO
    insurance_base: Decimal = ZERO
    duty_base: Decimal = ZERO
    customs_base: Decimal = ZERO
    packing_base: Decimal = ZERO
    tooling_amortized_base: Decimal = ZERO
    other_charges_base: Decimal = ZERO
    discount_base: Decimal = ZERO
    landed_cost_base: Decimal = ZERO
    payment_terms_raw: str = ""
    payment_terms_net_days: int = 30
    payment_terms_adjustment_base: Decimal = ZERO
    lead_time_days: int = 0
    lead_time_penalty_base: Decimal = ZERO
    quality_risk_adjustment_base: Decimal = ZERO
    total_cost_of_ownership_base: Decimal = ZERO
    normalized_unit_cost_base: Decimal = ZERO
    incoterm_from: str = ""
    incoterm_to: str = Incoterm.DAP.value
    base_currency: str = "USD"
    # Shipped weight, used to price freight on a per-kg lane rate.
    weight_kg: Decimal = ZERO
    adjustments: list[Adjustment] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    comparable: bool = True

    def to_row(self) -> dict[str, Any]:
        return {
            "quotation_id": self.quotation_id,
            "quotation_line_id": self.quotation_line_id,
            "vendor_id": self.vendor_id,
            "rfq_line_number": self.rfq_line_number,
            "quantity_base_uom": self.quantity_base_uom,
            "base_uom": self.base_uom,
            "quoted_unit_price": self.quoted_unit_price,
            "quoted_currency": self.quoted_currency,
            "fx_rate": self.fx_rate,
            "fx_as_of": self.fx_as_of,
            "unit_price_base": self.unit_price_base,
            "ext_price_base": self.ext_price_base,
            "freight_base": self.freight_base,
            "insurance_base": self.insurance_base,
            "duty_base": self.duty_base,
            "customs_base": self.customs_base,
            "packing_base": self.packing_base,
            "tooling_amortized_base": self.tooling_amortized_base,
            "other_charges_base": self.other_charges_base,
            "discount_base": self.discount_base,
            "landed_cost_base": self.landed_cost_base,
            "payment_terms_raw": self.payment_terms_raw,
            "payment_terms_net_days": self.payment_terms_net_days,
            "payment_terms_adjustment_base": self.payment_terms_adjustment_base,
            "lead_time_days": self.lead_time_days,
            "lead_time_penalty_base": self.lead_time_penalty_base,
            "quality_risk_adjustment_base": self.quality_risk_adjustment_base,
            "total_cost_of_ownership_base": self.total_cost_of_ownership_base,
            "normalized_unit_cost_base": self.normalized_unit_cost_base,
            "base_currency": self.base_currency,
            "incoterm_from": self.incoterm_from,
            "incoterm_to": self.incoterm_to,
            "adjustments": [a.to_dict() for a in self.adjustments],
            "assumptions": self.assumptions,
            "warnings": self.warnings,
            "comparable": self.comparable,
        }


@dataclass(slots=True)
class NormalizationResult:
    case_id: str
    negotiation_round: int
    lines: list[NormalizedLine] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def by_vendor(self) -> dict[str, list[NormalizedLine]]:
        out: dict[str, list[NormalizedLine]] = {}
        for line in self.lines:
            out.setdefault(line.vendor_id, []).append(line)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "negotiation_round": self.negotiation_round,
            "line_count": len(self.lines),
            "warnings": self.warnings,
            "vendors": {
                vendor_id: {
                    "landed_cost_base": str(sum(l.landed_cost_base for l in lines)),
                    "tco_base": str(sum(l.total_cost_of_ownership_base for l in lines)),
                    "lines": [
                        {
                            "rfq_line_number": l.rfq_line_number,
                            "quoted": f"{l.quoted_unit_price} {l.quoted_currency}",
                            "unit_price_base": str(l.unit_price_base),
                            "normalized_unit_cost_base": str(l.normalized_unit_cost_base),
                            "adjustments": [a.to_dict() for a in l.adjustments],
                            "warnings": l.warnings,
                        }
                        for l in lines
                    ],
                }
                for vendor_id, lines in self.by_vendor().items()
            },
        }


class CommercialNormalizationService:
    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx
        self.fx: FxRateTable | None = None
        self._shortest_lead_time: int | None = None

    def normalize_case(
        self, case_id: str, *, negotiation_round: int | None = None
    ) -> NormalizationResult:
        case = self.ctx.repos.cases.require(case_id)
        if not case.commercial_unlocked:
            from procureguard.domain.errors import SealedBidError

            raise SealedBidError(
                "Commercial normalisation requires the technical approval that unseals bids",
                case_id=case_id,
            )

        round_number = negotiation_round if negotiation_round is not None else case.negotiation_round
        result = NormalizationResult(case_id=case_id, negotiation_round=round_number)
        settings = self.ctx.settings
        self.fx = self.ctx.repos.fx.load_table(base_currency=settings.base_currency)

        rfq = self.ctx.repos.rfqs.latest_for_case(case_id)
        target_incoterm = (rfq.required_incoterm if rfq else Incoterm.DAP.value) or Incoterm.DAP.value
        plant_code = rfq.delivery_plant if rfq else case_id[:4]

        quotations = self.ctx.repos.quotations.list_for_case(
            case_id, commercial_unlocked=True, negotiation_round=round_number
        )
        if not quotations:
            quotations = self.ctx.repos.quotations.list_for_case(case_id, commercial_unlocked=True)

        # The lead-time penalty is relative, so the benchmark has to be the
        # fastest offer actually on the table - established before any line is
        # priced, or the first supplier normalised would set its own baseline.
        stated_lead_times = [
            int(q.lead_time_days)
            for q in quotations
            if q.lead_time_days and q.status not in ("WITHDRAWN", "SUPERSEDED", "QUARANTINED")
        ]
        self._shortest_lead_time = min(stated_lead_times) if stated_lead_times else None

        for quotation in quotations:
            if quotation.status in ("WITHDRAWN", "SUPERSEDED", "QUARANTINED"):
                continue
            result.lines.extend(
                self._normalize_quotation(quotation, target_incoterm, plant_code, result)
            )

        self.ctx.repos.normalized_offers.replace_for_round(
            case_id, round_number, [line.to_row() for line in result.lines]
        )
        self.ctx.repos.decisions.record(
            case_id=case_id,
            decision_type=DecisionType.COMMERCIAL_NORMALIZATION.value,
            recommendation=result.to_dict(),
            rationale=(
                f"Normalised {len(result.lines)} quoted line(s) from "
                f"{len(result.by_vendor())} supplier(s) to {settings.base_currency} "
                f"{target_incoterm} {plant_code}"
            ),
            confidence=Decimal("0.9"),
            model_metadata={
                "engine": "deterministic-commercial-normaliser-v1",
                "wacc_annual_pct": str(settings.wacc_annual_pct),
                "target_incoterm": target_incoterm,
            },
        )
        log.info(
            "commercial_normalization_completed",
            case_id=case_id,
            round=round_number,
            lines=len(result.lines),
            vendors=len(result.by_vendor()),
        )
        return result

    # ------------------------------------------------------------ per quotation
    def _normalize_quotation(
        self,
        quotation: Any,
        target_incoterm: str,
        plant_code: str,
        result: NormalizationResult,
    ) -> list[NormalizedLine]:
        settings = self.ctx.settings
        base_currency = settings.base_currency
        vendor = self.ctx.repos.vendors.get(quotation.vendor_id)
        lines = list(quotation.lines)
        if not lines:
            result.warnings.append(f"{quotation.vendor_id}: no priced lines to normalise")
            return []

        as_of = (quotation.received_at or datetime.now(UTC)).date()
        quote_currency = quotation.currency or base_currency

        # Line subtotals in base currency drive the allocation of header charges.
        prepared: list[tuple[Any, NormalizedLine]] = []
        for line in lines:
            normalized = self._normalize_line(
                quotation, line, base_currency, as_of, target_incoterm
            )
            prepared.append((line, normalized))

        subtotal = sum((n.ext_price_base for _, n in prepared), ZERO)
        if subtotal <= 0:
            result.warnings.append(
                f"{quotation.vendor_id}: quoted lines total zero; the bid is not comparable"
            )
            for _, normalized in prepared:
                normalized.comparable = False
            return [n for _, n in prepared]

        header_charges = self._header_charges(quotation, quote_currency, base_currency, as_of)
        lane = self._lane(vendor, plant_code)
        buyer_costs = INCOTERM_BUYER_COSTS.get(
            (quotation.incoterm or "").upper(), INCOTERM_BUYER_COSTS[Incoterm.EXW.value]
        )
        if not quotation.incoterm:
            for _, normalized in prepared:
                normalized.warnings.append(
                    "No Incoterm stated; EXW assumed, which is the most conservative reading "
                    "and adds the full landed-cost burden to the buyer"
                )

        for _line, normalized in prepared:
            share = normalized.ext_price_base / subtotal
            self._apply_header_charges(normalized, header_charges, share)
            self._apply_incoterm(normalized, lane, buyer_costs, share, quotation, vendor)
            self._apply_payment_terms(normalized, quotation)
            self._apply_lead_time(normalized, quotation)
            self._apply_quality_risk(normalized, vendor)
            self._finalise(normalized)
        return [n for _, n in prepared]

    def _normalize_line(
        self,
        quotation: Any,
        line: Any,
        base_currency: str,
        as_of: date,
        target_incoterm: str,
    ) -> NormalizedLine:
        converter = UnitConverter(
            self.ctx.repos.materials.get_alternate_units(line.material_code)
            if line.material_code
            else {}
        )
        material = self.ctx.repos.materials.get(line.material_code) if line.material_code else None
        base_uom = material.base_uom if material else (line.uom or "EA")

        quantity = Decimal(str(line.quantity or 0))
        unit_price = Decimal(str(line.unit_price or 0))
        per = Decimal(str(line.price_per_quantity or 1)) or Decimal(1)
        currency = (line.currency or quotation.currency or base_currency).upper()

        normalized = NormalizedLine(
            quotation_id=quotation.id,
            quotation_line_id=line.id,
            vendor_id=quotation.vendor_id,
            rfq_line_number=int(line.rfq_line_number or 0),
            quantity_base_uom=quantity,
            base_uom=base_uom,
            quoted_unit_price=unit_price,
            quoted_currency=currency,
            fx_rate=Decimal(1),
            fx_as_of=as_of,
            unit_price_base=ZERO,
            ext_price_base=ZERO,
            base_currency=base_currency,
            incoterm_from=(quotation.incoterm or Incoterm.EXW.value).upper(),
            incoterm_to=target_incoterm,
            payment_terms_raw=quotation.payment_terms or "",
        )

        # Price basis: "250 EUR per 100 pcs" becomes 2.50 EUR per piece.
        price_per_quoted_unit = unit_price / per
        if per != 1:
            normalized.assumptions.append(
                f"Quoted per {per} {line.uom}; unit price restated as "
                f"{price_per_quoted_unit} {currency} per {line.uom}"
            )

        # Unit of measure.
        quoted_uom = line.uom or base_uom
        if quoted_uom != base_uom:
            if converter.can_convert(quoted_uom, base_uom):
                factor = converter.factor(quoted_uom, base_uom)
                price_per_quoted_unit = price_per_quoted_unit / factor
                normalized.quantity_base_uom = converter.convert(quantity, quoted_uom, base_uom)
                normalized.assumptions.append(
                    f"Converted {quoted_uom} to {base_uom} at {factor} {base_uom} per {quoted_uom}"
                )
            else:
                normalized.comparable = False
                normalized.warnings.append(
                    f"Supplier quoted in {quoted_uom}, which cannot be converted to the "
                    f"material base unit {base_uom}; this line is not comparable and needs a "
                    f"material-master alternate unit or a clarification"
                )

        # Currency, at the rate on the day the quotation was received.
        if currency != base_currency:
            try:
                rate = self.fx.rate(currency, base_currency, as_of) if self.fx else Decimal(1)
                normalized.fx_rate = rate
                price_per_quoted_unit = price_per_quoted_unit * rate
                normalized.assumptions.append(
                    f"Converted {currency} to {base_currency} at {rate} as of {as_of}"
                )
            except Exception as exc:
                normalized.comparable = False
                normalized.warnings.append(
                    f"No {currency}->{base_currency} exchange rate available for {as_of}: {exc}"
                )

        normalized.unit_price_base = price_per_quoted_unit.quantize(Decimal("0.000001"))
        normalized.ext_price_base = (
            normalized.unit_price_base * normalized.quantity_base_uom
        ).quantize(Decimal("0.01"))

        if material is not None and material.net_weight_kg:
            normalized.weight_kg = (
                Decimal(str(material.net_weight_kg)) * normalized.quantity_base_uom
            )
        return normalized

    def _header_charges(
        self, quotation: Any, quote_currency: str, base_currency: str, as_of: date
    ) -> dict[str, Decimal]:
        def convert(amount: Any) -> Decimal:
            value = Decimal(str(amount or 0))
            if value == 0 or quote_currency == base_currency:
                return value
            try:
                return value * (self.fx.rate(quote_currency, base_currency, as_of) if self.fx else Decimal(1))
            except Exception:
                return value

        return {
            "freight": convert(quotation.freight_amount),
            "packing": convert(quotation.packing_amount),
            "tooling": convert(quotation.tooling_amount),
            "other": convert(quotation.other_charges),
            "discount": convert(quotation.discount_amount),
        }

    @staticmethod
    def _apply_header_charges(
        normalized: NormalizedLine, charges: dict[str, Decimal], share: Decimal
    ) -> None:
        """Allocate header-level charges across lines by value share."""
        mapping = (
            ("freight", "freight_base", "SELLER_FREIGHT", "Supplier-quoted freight"),
            ("packing", "packing_base", "PACKING", "Supplier-quoted packing"),
            ("other", "other_charges_base", "OTHER_CHARGES", "Other supplier charges"),
        )
        for key, attribute, code, label in mapping:
            amount = (charges[key] * share).quantize(Decimal("0.01"))
            if amount:
                setattr(normalized, attribute, getattr(normalized, attribute) + amount)
                normalized.adjustments.append(
                    Adjustment(code, label, amount, f"{(share * 100).quantize(Decimal('0.01'))}% of order value")
                )

        # Tooling is one-off; amortising it over this order is the conservative
        # treatment and is stated as an assumption rather than hidden.
        tooling = (charges["tooling"] * share).quantize(Decimal("0.01"))
        if tooling:
            normalized.tooling_amortized_base = tooling
            normalized.adjustments.append(
                Adjustment("TOOLING", "One-off tooling amortised over this order", tooling, "value share")
            )
            normalized.assumptions.append(
                "Tooling is charged in full against this order; if it is reusable, restate the "
                "comparison over the expected lifetime volume"
            )

        discount = (charges["discount"] * share).quantize(Decimal("0.01"))
        if discount:
            normalized.discount_base = discount
            normalized.adjustments.append(
                Adjustment("DISCOUNT", "Supplier discount", -discount, "value share")
            )

    def _apply_incoterm(
        self,
        normalized: NormalizedLine,
        lane: Any,
        buyer_costs: frozenset[str],
        share: Decimal,
        quotation: Any,
        vendor: Any,
    ) -> None:
        """Add the delivery costs the seller's Incoterm excluded."""
        if not buyer_costs:
            normalized.assumptions.append(
                f"{normalized.incoterm_from} is delivered duty paid; no buyer-side landed "
                f"costs are added"
            )
            return

        goods_value = normalized.ext_price_base
        weight_kg = normalized.weight_kg

        if lane is not None:
            lane_freight = (
                Decimal(str(lane.cost_per_kg or 0)) * weight_kg
                + Decimal(str(lane.cost_per_shipment or 0)) * share
            )
            duty_rate = Decimal(str(lane.duty_rate_pct or 0))
            insurance_rate = Decimal(str(lane.insurance_pct or 0))
            customs_cost = Decimal(str(lane.customs_clearance_cost or 0)) * share
            lane_label = f"{lane.origin_country}->{lane.destination_plant} by {lane.mode}"
        else:
            # No lane data: assume a percentage of goods value and say so loudly.
            lane_freight = goods_value * Decimal("0.06")
            duty_rate = Decimal(str(self.ctx.settings.default_duty_rate_pct))
            insurance_rate = Decimal("0.35")
            customs_cost = ZERO
            lane_label = "estimated (no freight rate on file)"
            normalized.warnings.append(
                f"No freight rate is on file for {vendor.country if vendor else 'this origin'}; "
                f"landed cost uses a 6% of goods value estimate and should be confirmed"
            )

        for component in ("origin_handling", "main_freight", "dest_delivery"):
            if component not in buyer_costs:
                continue
            amount = (lane_freight * FREIGHT_SPLIT[component]).quantize(Decimal("0.01"))
            if amount:
                normalized.freight_base += amount
                normalized.adjustments.append(
                    Adjustment(
                        component.upper(),
                        f"{component.replace('_', ' ').title()} not included in "
                        f"{normalized.incoterm_from}",
                        amount,
                        lane_label,
                    )
                )

        if "insurance" in buyer_costs:
            amount = (goods_value * insurance_rate / Decimal(100)).quantize(Decimal("0.01"))
            if amount:
                normalized.insurance_base = amount
                normalized.adjustments.append(
                    Adjustment("INSURANCE", "Cargo insurance", amount, f"{insurance_rate}% of goods value")
                )

        if "customs" in buyer_costs and customs_cost:
            normalized.customs_base = customs_cost.quantize(Decimal("0.01"))
            normalized.adjustments.append(
                Adjustment("CUSTOMS", "Import clearance", normalized.customs_base, lane_label)
            )

        if "duty" in buyer_costs and duty_rate:
            # Duty is levied on the CIF value, not the ex-works price.
            dutiable = goods_value + normalized.freight_base + normalized.insurance_base
            amount = (dutiable * duty_rate / Decimal(100)).quantize(Decimal("0.01"))
            if amount:
                normalized.duty_base = amount
                normalized.adjustments.append(
                    Adjustment("DUTY", "Import duty", amount, f"{duty_rate}% of CIF value")
                )

    def _apply_payment_terms(self, normalized: NormalizedLine, quotation: Any) -> None:
        """Price the credit the supplier is (or is not) extending."""
        terms = PaymentTerms.parse(quotation.payment_terms, default_net_days=30)
        normalized.payment_terms_net_days = terms.net_days
        gross = Money(
            normalized.ext_price_base
            + normalized.freight_base
            + normalized.insurance_base
            + normalized.duty_base
            + normalized.customs_base
            + normalized.packing_base
            + normalized.tooling_amortized_base
            + normalized.other_charges_base
            - normalized.discount_base,
            normalized.base_currency,
        )
        present_value = terms.present_value(gross, self.ctx.settings.daily_discount_rate)
        adjustment = (present_value.amount - gross.amount).quantize(Decimal("0.01"))
        normalized.payment_terms_adjustment_base = adjustment
        if adjustment:
            normalized.adjustments.append(
                Adjustment(
                    "PAYMENT_TERMS",
                    f"Time value of '{terms.raw or f'net {terms.net_days}'}' "
                    f"({terms.effective_days()} days effective)",
                    adjustment,
                    f"discounted at {self.ctx.settings.wacc_annual_pct}% WACC",
                )
            )
        if terms.requires_lc:
            normalized.warnings.append(
                "Supplier requires a letter of credit; bank charges and administrative effort "
                "are not included in this comparison"
            )

    def _apply_lead_time(self, normalized: NormalizedLine, quotation: Any) -> None:
        """Charge the inventory cost of a longer lead time.

        The benchmark is the shortest lead time available on the case, so this
        never penalises the whole field - only the gap between suppliers.
        """
        lead_days = int(quotation.lead_time_days or 0)
        normalized.lead_time_days = lead_days
        if lead_days <= 0:
            normalized.warnings.append("No lead time stated; delivery risk is unassessed")
            return
        benchmark = self._shortest_lead_time
        if benchmark is None or lead_days <= benchmark:
            return
        excess_days = Decimal(lead_days - benchmark)
        carrying = Decimal(str(self.ctx.settings.inventory_carrying_cost_annual_pct)) / Decimal(100)
        penalty = (
            normalized.ext_price_base * carrying * excess_days / Decimal(365)
        ).quantize(Decimal("0.01"))
        if penalty:
            normalized.lead_time_penalty_base = penalty
            normalized.adjustments.append(
                Adjustment(
                    "LEAD_TIME",
                    f"{int(excess_days)} days longer than the fastest offer",
                    penalty,
                    f"{self.ctx.settings.inventory_carrying_cost_annual_pct}% annual carrying cost",
                )
            )

    def _apply_quality_risk(self, normalized: NormalizedLine, vendor: Any) -> None:
        """Expected cost of the supplier's own historical defect rate."""
        if vendor is None:
            return
        ppm = Decimal(int(vendor.quality_ppm or 0))
        if ppm <= 0:
            return
        defect_rate = ppm / Decimal(1_000_000)
        # A rejected part costs its price plus the cost of dealing with it;
        # 2x replacement cost is the conventional planning figure.
        penalty = (normalized.ext_price_base * defect_rate * Decimal(2)).quantize(Decimal("0.01"))
        if penalty:
            normalized.quality_risk_adjustment_base = penalty
            normalized.adjustments.append(
                Adjustment(
                    "QUALITY_RISK",
                    f"Expected rework/replacement at {int(ppm)} ppm defect rate",
                    penalty,
                    "2x replacement cost of expected defects",
                )
            )

    @staticmethod
    def _finalise(normalized: NormalizedLine) -> None:
        normalized.landed_cost_base = (
            normalized.ext_price_base
            + normalized.freight_base
            + normalized.insurance_base
            + normalized.duty_base
            + normalized.customs_base
            + normalized.packing_base
            + normalized.tooling_amortized_base
            + normalized.other_charges_base
            - normalized.discount_base
        ).quantize(Decimal("0.01"))
        normalized.total_cost_of_ownership_base = (
            normalized.landed_cost_base
            + normalized.payment_terms_adjustment_base
            + normalized.lead_time_penalty_base
            + normalized.quality_risk_adjustment_base
        ).quantize(Decimal("0.01"))
        normalized.normalized_unit_cost_base = (
            normalized.total_cost_of_ownership_base / normalized.quantity_base_uom
            if normalized.quantity_base_uom
            else ZERO
        ).quantize(Decimal("0.000001"))

    def _lane(self, vendor: Any, plant_code: str) -> Any:
        if vendor is None or not plant_code:
            return None
        return self.ctx.repos.freight.get_lane(vendor.country, plant_code)
