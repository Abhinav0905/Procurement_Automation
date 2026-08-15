"""Stage 13 - L1/L2/L3 calculation.

Produces the bid tabulation a procurement committee actually signs.

Two rankings are computed, because they answer different questions:

* **Cost ranking** - L1/L2/L3 by total cost of ownership. This is what "L1"
  means in practice and what the audit file needs.
* **Value ranking** - cost weighted against technical score, which is what a
  buyer uses to argue for paying 4% more for a supplier who is 20 points better
  technically.

Technically disqualified bids are ranked separately and never take an L
position. A cheap non-compliant bid is not L1; it is not a bid.

Partial offers are handled explicitly: a supplier who quoted 3 of 5 lines is
marked as partial and compared on the lines they quoted, so their price is not
silently treated as a complete offer.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from procureguard.application.commercial_normalization import NormalizationResult
from procureguard.application.history_service import PriceBenchmark
from procureguard.domain.enums import DecisionType
from procureguard.infrastructure.factory import ServiceContext
from procureguard.observability import logger

log = logger(__name__)

ZERO = Decimal(0)
POSITION_LABELS = ("L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8", "L9", "L10")

# Weighting for the value ranking. Cost dominates, but not absolutely.
COST_WEIGHT = Decimal(70)
TECHNICAL_WEIGHT = Decimal(30)


@dataclass(slots=True)
class RankedBid:
    vendor_id: str
    vendor_name: str
    quotation_id: str
    position: int
    position_label: str
    total_base: Decimal
    landed_cost_base: Decimal
    tco_base: Decimal
    delta_vs_l1_base: Decimal
    delta_vs_l1_pct: Decimal
    delta_vs_benchmark_pct: Decimal | None
    technical_score: Decimal | None
    weighted_value_score: Decimal | None
    technically_qualified: bool
    lines_covered: int
    lines_total: int
    partial_offer: bool
    flags: list[str] = field(default_factory=list)
    notes: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "vendor_id": self.vendor_id,
            "vendor_name": self.vendor_name,
            "quotation_id": self.quotation_id,
            "position": self.position,
            "position_label": self.position_label,
            "total_base": self.total_base,
            "landed_cost_base": self.landed_cost_base,
            "tco_base": self.tco_base,
            "delta_vs_l1_base": self.delta_vs_l1_base,
            "delta_vs_l1_pct": self.delta_vs_l1_pct,
            "delta_vs_benchmark_pct": self.delta_vs_benchmark_pct,
            "technical_score": self.technical_score,
            "weighted_value_score": self.weighted_value_score,
            "technically_qualified": self.technically_qualified,
            "lines_covered": self.lines_covered,
            "lines_total": self.lines_total,
            "partial_offer": self.partial_offer,
            "flags": self.flags,
            "notes": self.notes,
        }


@dataclass(slots=True)
class RankingResult:
    case_id: str
    ranking_run_id: str
    negotiation_round: int
    basis: str
    base_currency: str
    bids: list[RankedBid] = field(default_factory=list)
    disqualified: list[RankedBid] = field(default_factory=list)
    value_order: list[str] = field(default_factory=list)
    split_award: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def l1(self) -> RankedBid | None:
        return self.bids[0] if self.bids else None

    @property
    def best_value(self) -> RankedBid | None:
        if not self.value_order:
            return self.l1
        by_vendor = {b.vendor_id: b for b in self.bids}
        return by_vendor.get(self.value_order[0])

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "ranking_run_id": self.ranking_run_id,
            "negotiation_round": self.negotiation_round,
            "basis": self.basis,
            "base_currency": self.base_currency,
            "bids": [
                {
                    **{
                        k: (str(v) if isinstance(v, Decimal) else v)
                        for k, v in b.to_row().items()
                    }
                }
                for b in self.bids
            ],
            "disqualified": [
                {
                    "vendor_id": b.vendor_id,
                    "vendor_name": b.vendor_name,
                    "tco_base": str(b.tco_base),
                    "flags": b.flags,
                    "notes": b.notes,
                }
                for b in self.disqualified
            ],
            "value_order": self.value_order,
            "split_award": self.split_award,
            "warnings": self.warnings,
        }


class BidRankingService:
    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx

    def rank(
        self,
        *,
        case_id: str,
        normalization: NormalizationResult,
        benchmarks: dict[str, PriceBenchmark] | None = None,
        basis: str = "TCO",
    ) -> RankingResult:
        settings = self.ctx.settings
        result = RankingResult(
            case_id=case_id,
            ranking_run_id=str(uuid.uuid4()),
            negotiation_round=normalization.negotiation_round,
            basis=basis,
            base_currency=settings.base_currency,
        )
        benchmarks = benchmarks or {}

        rfq = self.ctx.repos.rfqs.latest_for_case(case_id)
        lines_total = len(rfq.lines) if rfq else 0
        # Benchmarks are keyed by material, offers by RFQ line; this is the join.
        material_by_line: dict[int, str] = (
            {line.line_number: line.material_code for line in rfq.lines} if rfq else {}
        )
        quotations = {
            q.vendor_id: q
            for q in self.ctx.repos.quotations.list_for_case(case_id, commercial_unlocked=True)
        }

        candidates: list[RankedBid] = []
        for vendor_id, lines in normalization.by_vendor().items():
            quotation = quotations.get(vendor_id)
            comparable = [line for line in lines if line.comparable]
            if not comparable:
                result.warnings.append(
                    f"{vendor_id}: no comparable lines after normalisation; excluded from ranking"
                )
                continue

            landed = sum((line.landed_cost_base for line in comparable), ZERO)
            tco = sum((line.total_cost_of_ownership_base for line in comparable), ZERO)
            total = tco if basis == "TCO" else landed
            covered = len({line.rfq_line_number for line in comparable})

            flags: list[str] = []
            if lines_total and covered < lines_total:
                flags.append(f"PARTIAL_OFFER_{covered}_OF_{lines_total}_LINES")
            for line in comparable:
                flags.extend(f"LINE_{line.rfq_line_number}:{w[:60]}" for w in line.warnings)

            benchmark_delta = self._benchmark_delta(comparable, benchmarks, material_by_line)
            for line in comparable:
                reference = _benchmark_price(line, benchmarks, material_by_line)
                if reference is None:
                    continue
                flags.extend(
                    f"LINE_{line.rfq_line_number}:{flag}"
                    for flag in self.ctx.policy.price_variance_flags(
                        quoted_unit_price=line.normalized_unit_cost_base,
                        benchmark_unit_price=reference,
                    )
                )

            if quotation is not None and quotation.valid_until:
                from datetime import UTC, datetime

                if quotation.valid_until < datetime.now(UTC):
                    flags.append("QUOTATION_EXPIRED")

            candidates.append(
                RankedBid(
                    vendor_id=vendor_id,
                    vendor_name=quotation.vendor_name if quotation else vendor_id,
                    quotation_id=quotation.id if quotation else "",
                    position=0,
                    position_label="",
                    total_base=total.quantize(Decimal("0.01")),
                    landed_cost_base=landed.quantize(Decimal("0.01")),
                    tco_base=tco.quantize(Decimal("0.01")),
                    delta_vs_l1_base=ZERO,
                    delta_vs_l1_pct=ZERO,
                    delta_vs_benchmark_pct=benchmark_delta,
                    technical_score=(
                        Decimal(str(quotation.technical_score))
                        if quotation is not None and quotation.technical_score is not None
                        else None
                    ),
                    weighted_value_score=None,
                    technically_qualified=bool(
                        quotation.technically_qualified if quotation is not None else False
                    ),
                    lines_covered=covered,
                    lines_total=lines_total or covered,
                    partial_offer=bool(lines_total and covered < lines_total),
                    flags=sorted(set(flags)),
                    notes="; ".join(
                        sorted({a.label for line in comparable for a in line.adjustments})
                    )[:2000],
                )
            )

        qualified = [b for b in candidates if b.technically_qualified]
        disqualified = [b for b in candidates if not b.technically_qualified]

        if not qualified and candidates:
            result.warnings.append(
                "No technically qualified bid to rank. Ranking is shown for information only "
                "and must not be used to award until a deviation is approved or the "
                "requirement is relaxed."
            )
            qualified = []

        qualified.sort(key=lambda b: (b.partial_offer, b.total_base))
        for index, bid in enumerate(qualified):
            bid.position = index + 1
            bid.position_label = (
                POSITION_LABELS[index] if index < len(POSITION_LABELS) else f"L{index + 1}"
            )
        if qualified:
            l1_total = qualified[0].total_base
            for bid in qualified:
                bid.delta_vs_l1_base = (bid.total_base - l1_total).quantize(Decimal("0.01"))
                bid.delta_vs_l1_pct = (
                    (bid.delta_vs_l1_base / l1_total * Decimal(100)).quantize(Decimal("0.0001"))
                    if l1_total
                    else ZERO
                )

        disqualified.sort(key=lambda b: b.total_base)
        for bid in disqualified:
            bid.position = 0
            bid.position_label = "DQ"

        self._compute_value_scores(qualified, result)
        result.bids = qualified
        result.disqualified = disqualified
        result.split_award = self._evaluate_split_award(normalization, qualified, lines_total)
        self._add_warnings(result, lines_total)

        self.ctx.repos.rankings.save_run(
            case_id,
            result.ranking_run_id,
            [
                {**bid.to_row(), "negotiation_round": result.negotiation_round, "basis": basis}
                for bid in (qualified + disqualified)
            ],
        )
        self.ctx.repos.decisions.record(
            case_id=case_id,
            decision_type=DecisionType.BID_RANKING.value,
            recommendation=result.to_dict(),
            rationale=_rationale(result),
            confidence=Decimal("0.92") if qualified else Decimal("0.3"),
            model_metadata={
                "engine": "deterministic-bid-ranking-v1",
                "basis": basis,
                "cost_weight": str(COST_WEIGHT),
                "technical_weight": str(TECHNICAL_WEIGHT),
            },
            evidence=[
                {
                    "evidence_type": "QUOTATION",
                    "evidence_id": bid.quotation_id,
                    "role": "SUPPORTS" if bid.position == 1 else "CONTEXT",
                    "excerpt": f"{bid.position_label}: {bid.total_base} {result.base_currency}",
                    "weight": bid.total_base,
                }
                for bid in qualified[:10]
            ],
        )
        self.ctx.audit(
            entity_type="BID_RANKING",
            entity_id=result.ranking_run_id,
            case_id=case_id,
            action="BIDS_RANKED",
            after_state={
                "l1": result.l1.vendor_id if result.l1 else None,
                "ranked": len(qualified),
                "disqualified": len(disqualified),
            },
        )
        log.info(
            "bids_ranked",
            case_id=case_id,
            ranked=len(qualified),
            disqualified=len(disqualified),
            l1=result.l1.vendor_id if result.l1 else None,
        )
        return result

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _compute_value_scores(bids: list[RankedBid], result: RankingResult) -> None:
        """Cost and technical score on one 0-100 scale.

        Cost is scored relative to the cheapest bid so the scale is meaningful
        regardless of absolute value; a bid 10% above L1 scores 90 on cost.
        """
        scored = [b for b in bids if b.technical_score is not None and b.total_base > 0]
        if not scored:
            result.value_order = [b.vendor_id for b in bids]
            return
        cheapest = min(b.total_base for b in scored)
        for bid in scored:
            cost_score = (cheapest / bid.total_base * Decimal(100)).quantize(Decimal("0.01"))
            bid.weighted_value_score = (
                (cost_score * COST_WEIGHT + (bid.technical_score or ZERO) * TECHNICAL_WEIGHT)
                / Decimal(100)
            ).quantize(Decimal("0.01"))
        result.value_order = [
            b.vendor_id
            for b in sorted(scored, key=lambda b: b.weighted_value_score or ZERO, reverse=True)
        ]

    @staticmethod
    def _benchmark_delta(
        lines: list[Any],
        benchmarks: dict[str, PriceBenchmark],
        material_by_line: dict[int, str],
    ) -> Decimal | None:
        """Value-weighted variance of this bid against historical prices.

        Weighted by line value rather than averaged flat, so a 40% overprice on
        a washer does not outweigh a 2% overprice on the pump.
        """
        weighted_sum = ZERO
        weight_total = ZERO
        for line in lines:
            reference = _benchmark_price(line, benchmarks, material_by_line)
            if reference is None or not line.normalized_unit_cost_base:
                continue
            delta = (line.normalized_unit_cost_base - reference) / reference * Decimal(100)
            weight = line.ext_price_base or ZERO
            weighted_sum += delta * weight
            weight_total += weight
        if weight_total <= 0:
            return None
        return (weighted_sum / weight_total).quantize(Decimal("0.0001"))

    @staticmethod
    def _evaluate_split_award(
        normalization: NormalizationResult, bids: list[RankedBid], lines_total: int
    ) -> dict[str, Any]:
        """Would awarding line-by-line beat awarding the whole order to L1?

        Frequently yes, and it is the cheapest saving available - but it costs
        an extra supplier relationship per line, so the recommendation is only
        made when the saving is material.
        """
        if len(bids) < 2 or lines_total < 2:
            return {"beneficial": False, "reason": "Split award needs at least two bids and two lines"}

        qualified_vendors = {b.vendor_id for b in bids}
        best_by_line: dict[int, tuple[str, Decimal]] = {}
        for vendor_id, lines in normalization.by_vendor().items():
            if vendor_id not in qualified_vendors:
                continue
            for line in lines:
                if not line.comparable:
                    continue
                current = best_by_line.get(line.rfq_line_number)
                if current is None or line.total_cost_of_ownership_base < current[1]:
                    best_by_line[line.rfq_line_number] = (
                        vendor_id,
                        line.total_cost_of_ownership_base,
                    )
        if not best_by_line:
            return {"beneficial": False, "reason": "No comparable lines"}

        split_total = sum(total for _, total in best_by_line.values())
        single_total = bids[0].total_base
        saving = single_total - split_total
        saving_pct = (saving / single_total * Decimal(100)).quantize(Decimal("0.01")) if single_total else ZERO
        vendors = sorted({vendor for vendor, _ in best_by_line.values()})
        return {
            "beneficial": bool(saving > 0 and saving_pct >= Decimal("1.5") and len(vendors) > 1),
            "split_total_base": str(split_total.quantize(Decimal("0.01"))),
            "single_source_total_base": str(single_total),
            "saving_base": str(saving.quantize(Decimal("0.01"))),
            "saving_pct": str(saving_pct),
            "supplier_count": len(vendors),
            "allocation": {
                str(line_number): {"vendor_id": vendor, "tco_base": str(total.quantize(Decimal("0.01")))}
                for line_number, (vendor, total) in sorted(best_by_line.items())
            },
            "reason": (
                f"Awarding each line to its cheapest qualified supplier saves "
                f"{saving_pct}% but involves {len(vendors)} suppliers"
                if saving > 0
                else "Single-source award to L1 is already cheapest"
            ),
        }

    def _add_warnings(self, result: RankingResult, lines_total: int) -> None:
        if len(result.bids) == 1:
            result.warnings.append(
                "Only one technically qualified bid; there is no competitive tension and a "
                "single-source justification is required"
            )
        elif len(result.bids) >= 2:
            spread = result.bids[-1].delta_vs_l1_pct
            if spread < Decimal("2"):
                result.warnings.append(
                    f"All bids are within {spread}% of each other; the field is tightly "
                    f"clustered and price is unlikely to be the deciding factor"
                )
            elif spread > Decimal("40"):
                result.warnings.append(
                    f"Bid spread is {spread}%, which usually means the suppliers priced "
                    f"different scopes; verify that every bid covers the same requirement"
                )
        for bid in result.bids:
            if bid.partial_offer:
                result.warnings.append(
                    f"{bid.vendor_id} quoted only {bid.lines_covered} of {bid.lines_total} lines; "
                    f"their total is not directly comparable to a complete offer"
                )
            if "QUOTATION_EXPIRED" in bid.flags:
                result.warnings.append(
                    f"{bid.vendor_id}'s quotation validity has lapsed; reconfirm the price "
                    f"before award"
                )
        if result.disqualified and result.bids:
            cheapest_dq = result.disqualified[0]
            if cheapest_dq.total_base < result.bids[0].total_base:
                result.warnings.append(
                    f"{cheapest_dq.vendor_id} is cheaper than L1 at "
                    f"{cheapest_dq.total_base} {result.base_currency} but is technically "
                    f"disqualified; awarding to them would require an approved deviation"
                )


def _benchmark_price(
    line: Any, benchmarks: dict[str, PriceBenchmark], material_by_line: dict[int, str]
) -> Decimal | None:
    """Historical unit price for the material this offer line answers."""
    material_code = material_by_line.get(int(line.rfq_line_number or 0), "")
    benchmark = benchmarks.get(material_code)
    if benchmark is None:
        return None
    reference = benchmark.benchmark_unit_price
    return reference if reference and reference > 0 else None


def _rationale(result: RankingResult) -> str:
    if not result.bids:
        return "No technically qualified bid could be ranked"
    l1 = result.bids[0]
    parts = [
        f"L1 is {l1.vendor_id} at {l1.total_base} {result.base_currency} "
        f"on a {result.basis} basis"
    ]
    if len(result.bids) > 1:
        l2 = result.bids[1]
        parts.append(
            f"L2 {l2.vendor_id} is {l2.delta_vs_l1_pct}% higher "
            f"({l2.delta_vs_l1_base} {result.base_currency})"
        )
    best_value = result.best_value
    if best_value and l1 and best_value.vendor_id != l1.vendor_id:
        parts.append(
            f"On a weighted value basis {best_value.vendor_id} scores higher "
            f"({best_value.weighted_value_score}) despite costing "
            f"{best_value.delta_vs_l1_pct}% more"
        )
    if result.split_award.get("beneficial"):
        parts.append(
            f"A line-by-line split award would save a further "
            f"{result.split_award['saving_pct']}%"
        )
    return "; ".join(parts)
