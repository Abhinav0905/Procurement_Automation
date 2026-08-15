"""Stage 6 - supplier shortlisting.

Produces a ranked, fully-explained candidate list. Every component score is
stored, so a buyer can see *why* a supplier ranked where it did and override it
with a reason rather than silently.

Candidates come from four sources, in decreasing order of authority:

1. the source list (approved vendor list) for the material and plant
2. suppliers who have actually delivered this material before
3. suppliers active in the same material group (adjacent capability)
4. vector similarity between the requirement text and supplier capability text

A fixed source on the source list short-circuits the whole thing: if master data
says there is one approved supplier, competition is a master-data change, not an
agent decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from procureguard.application.history_service import PriceBenchmark
from procureguard.domain.enums import DecisionType, RiskLevel
from procureguard.infrastructure.db.models import VendorModel
from procureguard.infrastructure.factory import ServiceContext
from procureguard.observability import logger

log = logger(__name__)

# Weights sum to 100. Tuned so that demonstrated delivery performance cannot be
# outvoted by a good price alone - the most expensive procurement failures are
# late and defective deliveries, not overpayment.
WEIGHTS = {
    "history": Decimal(25),      # has supplied this exact material
    "performance": Decimal(25),  # on-time delivery and quality
    "capability": Decimal(20),   # certifications, tags, category presence
    "commercial": Decimal(15),   # historical price competitiveness
    "risk": Decimal(10),         # financial / geopolitical / qualification
    "responsiveness": Decimal(5),  # answers RFQs at all
}


@dataclass(slots=True)
class Candidate:
    vendor_id: str
    vendor_name: str
    total_score: Decimal = Decimal(0)
    history_score: Decimal = Decimal(0)
    performance_score: Decimal = Decimal(0)
    capability_score: Decimal = Decimal(0)
    commercial_score: Decimal = Decimal(0)
    risk_score: Decimal = Decimal(0)
    responsiveness_score: Decimal = Decimal(0)
    similarity_score: Decimal = Decimal(0)
    sources: set[str] = field(default_factory=set)
    reasons: list[str] = field(default_factory=list)
    excluded_reason: str = ""
    rank: int = 0
    selected: bool = False
    last_purchase_date: datetime | None = None
    last_unit_price_base: Decimal | None = None
    purchase_count_36m: int = 0
    breakdown: dict[str, Any] = field(default_factory=dict)

    @property
    def is_excluded(self) -> bool:
        return bool(self.excluded_reason)

    def to_row(self) -> dict[str, Any]:
        return {
            "vendor_id": self.vendor_id,
            "vendor_name": self.vendor_name,
            "rank": self.rank,
            "total_score": self.total_score,
            "history_score": self.history_score,
            "performance_score": self.performance_score,
            "capability_score": self.capability_score,
            "commercial_score": self.commercial_score,
            "risk_score": self.risk_score,
            "responsiveness_score": self.responsiveness_score,
            "similarity_score": self.similarity_score,
            "score_breakdown": self.breakdown,
            "rationale": "; ".join(self.reasons),
            "selection_source": "+".join(sorted(self.sources)) or "SCORED",
            "selected": self.selected,
            "excluded_reason": self.excluded_reason,
            "last_purchase_date": self.last_purchase_date,
            "last_unit_price_base": self.last_unit_price_base,
            "purchase_count_36m": self.purchase_count_36m,
        }


@dataclass(slots=True)
class ShortlistResult:
    case_id: str
    material_code: str
    candidates: list[Candidate] = field(default_factory=list)
    selected_vendor_ids: list[str] = field(default_factory=list)
    is_single_source: bool = False
    single_source_reason: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "material_code": self.material_code,
            "selected_vendor_ids": self.selected_vendor_ids,
            "is_single_source": self.is_single_source,
            "single_source_reason": self.single_source_reason,
            "warnings": self.warnings,
            "candidates": [
                {
                    "vendor_id": c.vendor_id,
                    "vendor_name": c.vendor_name,
                    "rank": c.rank,
                    "total_score": str(c.total_score),
                    "selected": c.selected,
                    "sources": sorted(c.sources),
                    "rationale": "; ".join(c.reasons),
                    "excluded_reason": c.excluded_reason,
                    "breakdown": c.breakdown,
                }
                for c in self.candidates
            ],
        }


class SupplierShortlistService:
    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx
        self.vendors = ctx.repos.vendors
        self.history = ctx.repos.history
        self.materials = ctx.repos.materials

    def build(
        self,
        *,
        case_id: str,
        material_code: str,
        plant_code: str = "",
        benchmark: PriceBenchmark | None = None,
        requirement_text: str = "",
        preferred_vendor_id: str = "",
        max_suppliers: int | None = None,
    ) -> ShortlistResult:
        result = ShortlistResult(case_id=case_id, material_code=material_code)
        limit = max_suppliers or self.ctx.settings.max_suppliers_per_rfq

        fixed = self.vendors.fixed_source_for_material(material_code, plant_code)
        candidates: dict[str, Candidate] = {}

        self._add_source_list(candidates, material_code, plant_code)
        self._add_historical(candidates, material_code)
        self._add_category(candidates, material_code)
        self._add_semantic(candidates, requirement_text or material_code)
        if preferred_vendor_id:
            self._add_preferred(candidates, preferred_vendor_id)

        if not candidates:
            result.warnings.append(
                f"No supplier could be identified for {material_code}. Source the market "
                f"manually or extend the approved vendor list before issuing an RFQ."
            )
            self._persist(case_id, result)
            return result

        vendor_rows = self.vendors.get_many(list(candidates))
        for vendor_id, candidate in list(candidates.items()):
            vendor = vendor_rows.get(vendor_id)
            if vendor is None:
                candidates.pop(vendor_id)
                continue
            self._score(candidate, vendor, material_code, benchmark)

        ranked = sorted(
            candidates.values(),
            key=lambda c: (not c.is_excluded, c.total_score),
            reverse=True,
        )
        for position, candidate in enumerate(ranked, start=1):
            candidate.rank = position

        eligible = [c for c in ranked if not c.is_excluded]

        if fixed and fixed in candidates:
            for candidate in ranked:
                candidate.selected = candidate.vendor_id == fixed
            result.is_single_source = True
            result.single_source_reason = (
                f"{fixed} is a fixed source on the source list for {material_code}; "
                f"competitive bidding requires a master-data change"
            )
            result.warnings.append(result.single_source_reason)
        else:
            for candidate in eligible[:limit]:
                candidate.selected = True

        result.candidates = ranked
        result.selected_vendor_ids = [c.vendor_id for c in ranked if c.selected]

        minimum = self.ctx.settings.min_suppliers_per_rfq
        if not result.is_single_source and len(result.selected_vendor_ids) < minimum:
            result.is_single_source = len(result.selected_vendor_ids) <= 1
            result.warnings.append(
                f"Only {len(result.selected_vendor_ids)} qualified supplier(s) available "
                f"against a competitive minimum of {minimum}; a documented "
                f"single/limited-source justification will be required at award"
            )
        excluded = [c for c in ranked if c.is_excluded]
        if excluded:
            result.warnings.append(
                f"{len(excluded)} known supplier(s) excluded: "
                + "; ".join(f"{c.vendor_id} ({c.excluded_reason})" for c in excluded[:5])
            )

        self._persist(case_id, result)
        return result

    # --------------------------------------------------------------- sourcing
    def _add_source_list(
        self, candidates: dict[str, Candidate], material_code: str, plant_code: str
    ) -> None:
        for vendor_id in self.vendors.approved_for_material(material_code, plant_code):
            candidate = _ensure(candidates, vendor_id)
            candidate.sources.add("SOURCE_LIST")
            candidate.reasons.append("On the approved source list for this material and plant")

    def _add_historical(self, candidates: dict[str, Candidate], material_code: str) -> None:
        for row in self.history.get_vendors_for_material(material_code, months=36, limit=20):
            candidate = _ensure(candidates, row["vendor_id"], row["vendor_name"])
            candidate.sources.add("PURCHASE_HISTORY")
            candidate.purchase_count_36m = row["order_count"]
            candidate.last_unit_price_base = row["weighted_avg_unit_price"]
            candidate.last_purchase_date = _parse_iso(row["last_order_date"])
            candidate.reasons.append(
                f"Supplied this material {row['order_count']} time(s) in the last 36 months"
            )

    def _add_category(self, candidates: dict[str, Candidate], material_code: str) -> None:
        material = self.materials.get(material_code)
        if material is None or not material.material_group:
            return
        for row in self.history.get_vendors_by_material_group(
            material.material_group, months=36, limit=20
        ):
            if row["vendor_id"] in candidates:
                continue
            candidate = _ensure(candidates, row["vendor_id"], row["vendor_name"])
            candidate.sources.add("CATEGORY")
            candidate.reasons.append(
                f"Supplies {row['material_count']} other material(s) in group "
                f"{material.material_group}"
            )

    def _add_semantic(self, candidates: dict[str, Candidate], query: str) -> None:
        if not query.strip():
            return
        try:
            vector = self.ctx.embedder.embed(query)
            hits = self.vendors.semantic_search(
                vector, top_k=10, dimensions=self.ctx.embedder.dimensions
            )
        except Exception as exc:
            log.info("vendor_semantic_search_unavailable", detail=str(exc)[:200])
            return
        for vendor_id, score in hits:
            if score < 0.25:
                continue
            candidate = _ensure(candidates, vendor_id)
            candidate.similarity_score = Decimal(str(round(score, 4)))
            if "SOURCE_LIST" not in candidate.sources and "PURCHASE_HISTORY" not in candidate.sources:
                candidate.sources.add("CAPABILITY_MATCH")
                candidate.reasons.append(
                    f"Capability profile matches the requirement (similarity {score:.2f})"
                )

    def _add_preferred(self, candidates: dict[str, Candidate], vendor_id: str) -> None:
        candidate = _ensure(candidates, vendor_id)
        candidate.sources.add("REQUESTER_PREFERRED")
        candidate.reasons.append("Nominated by the requester on the requisition")

    # ---------------------------------------------------------------- scoring
    def _score(
        self,
        candidate: Candidate,
        vendor: VendorModel,
        material_code: str,
        benchmark: PriceBenchmark | None,
    ) -> None:
        candidate.vendor_name = vendor.name

        # Hard exclusions first: no score can rescue a blocked supplier.
        if vendor.status == "BLOCKED":
            candidate.excluded_reason = f"Vendor is BLOCKED: {vendor.blocked_reason or 'no reason recorded'}"
        elif vendor.status == "DEREGISTERED":
            candidate.excluded_reason = "Vendor is deregistered"
        elif not vendor.qualified:
            candidate.excluded_reason = "Vendor is not qualified"
        elif vendor.qualification_expires_on and vendor.qualification_expires_on < datetime.now(UTC):
            candidate.excluded_reason = (
                f"Qualification expired on {vendor.qualification_expires_on.date()}"
            )
        if candidate.excluded_reason:
            candidate.total_score = Decimal(0)
            return

        history = self._history_score(candidate)
        performance = self._performance_score(vendor)
        capability = self._capability_score(candidate, vendor)
        commercial = self._commercial_score(candidate, benchmark)
        risk = self._risk_score(vendor)
        responsiveness = self._responsiveness_score(vendor)

        candidate.history_score = history
        candidate.performance_score = performance
        candidate.capability_score = capability
        candidate.commercial_score = commercial
        candidate.risk_score = risk
        candidate.responsiveness_score = responsiveness

        candidate.total_score = sum(
            (
                history * WEIGHTS["history"],
                performance * WEIGHTS["performance"],
                capability * WEIGHTS["capability"],
                commercial * WEIGHTS["commercial"],
                risk * WEIGHTS["risk"],
                responsiveness * WEIGHTS["responsiveness"],
            ),
            Decimal(0),
        ) / Decimal(100)
        candidate.total_score = candidate.total_score.quantize(Decimal("0.0001"))

        candidate.breakdown = {
            "components": {
                "history": {"score": str(history), "weight": str(WEIGHTS["history"])},
                "performance": {"score": str(performance), "weight": str(WEIGHTS["performance"])},
                "capability": {"score": str(capability), "weight": str(WEIGHTS["capability"])},
                "commercial": {"score": str(commercial), "weight": str(WEIGHTS["commercial"])},
                "risk": {"score": str(risk), "weight": str(WEIGHTS["risk"])},
                "responsiveness": {
                    "score": str(responsiveness),
                    "weight": str(WEIGHTS["responsiveness"]),
                },
            },
            "vendor_facts": {
                "on_time_delivery_pct": str(vendor.on_time_delivery_pct),
                "quality_ppm": vendor.quality_ppm,
                "country": vendor.country,
                "financial_risk": vendor.financial_risk,
                "iso9001": vendor.iso9001_certified,
                "quote_response_rate_pct": str(vendor.quote_response_rate_pct),
            },
            "total": str(candidate.total_score),
        }

    @staticmethod
    def _history_score(candidate: Candidate) -> Decimal:
        """Saturating: five orders proves capability; fifty proves it no harder."""
        if "SOURCE_LIST" in candidate.sources:
            base = Decimal("0.7")
        elif "PURCHASE_HISTORY" in candidate.sources:
            base = Decimal("0.5")
        elif "CATEGORY" in candidate.sources:
            base = Decimal("0.25")
        else:
            base = Decimal("0.1")
        volume_bonus = min(Decimal(candidate.purchase_count_36m) / Decimal(5), Decimal(1)) * Decimal("0.3")
        return min(base + volume_bonus, Decimal(1))

    @staticmethod
    def _performance_score(vendor: VendorModel) -> Decimal:
        otd = Decimal(str(vendor.on_time_delivery_pct or 0)) / Decimal(100)
        # 0 ppm -> 1.0, 5000 ppm -> 0.0, linear in between.
        ppm = max(Decimal(0), Decimal(1) - Decimal(int(vendor.quality_ppm or 0)) / Decimal(5000))
        return (otd * Decimal("0.6") + ppm * Decimal("0.4")).quantize(Decimal("0.0001"))

    @staticmethod
    def _capability_score(candidate: Candidate, vendor: VendorModel) -> Decimal:
        score = Decimal("0.3")
        if vendor.iso9001_certified:
            score += Decimal("0.25")
        if vendor.iso14001_certified:
            score += Decimal("0.1")
        if vendor.iatf16949_certified:
            score += Decimal("0.15")
        score += min(candidate.similarity_score, Decimal(1)) * Decimal("0.2")
        return min(score, Decimal(1)).quantize(Decimal("0.0001"))

    @staticmethod
    def _commercial_score(candidate: Candidate, benchmark: PriceBenchmark | None) -> Decimal:
        """How this supplier's historical price compares to the market band.

        No history means neutral, not zero: a new supplier is unproven, not
        expensive, and scoring them to zero would entrench the incumbent.
        """
        if (
            benchmark is None
            or candidate.last_unit_price_base is None
            or benchmark.min_unit_price is None
            or benchmark.max_unit_price is None
        ):
            return Decimal("0.5")
        low, high = benchmark.min_unit_price, benchmark.max_unit_price
        if high <= low:
            return Decimal("0.75")
        position = (candidate.last_unit_price_base - low) / (high - low)
        return max(Decimal(0), min(Decimal(1), Decimal(1) - position)).quantize(Decimal("0.0001"))

    @staticmethod
    def _risk_score(vendor: VendorModel) -> Decimal:
        levels = {
            RiskLevel.LOW.value: Decimal(1),
            RiskLevel.MEDIUM.value: Decimal("0.6"),
            RiskLevel.HIGH.value: Decimal("0.25"),
            RiskLevel.CRITICAL.value: Decimal(0),
        }
        financial = levels.get(vendor.financial_risk, Decimal("0.5"))
        geopolitical = levels.get(vendor.geopolitical_risk, Decimal("0.5"))
        return ((financial + geopolitical) / 2).quantize(Decimal("0.0001"))

    @staticmethod
    def _responsiveness_score(vendor: VendorModel) -> Decimal:
        rate = Decimal(str(vendor.quote_response_rate_pct or 0)) / Decimal(100)
        turnaround = Decimal(str(vendor.average_quote_turnaround_days or 10))
        speed = max(Decimal(0), Decimal(1) - turnaround / Decimal(14))
        return (rate * Decimal("0.7") + speed * Decimal("0.3")).quantize(Decimal("0.0001"))

    # -------------------------------------------------------------- persistence
    def _persist(self, case_id: str, result: ShortlistResult) -> None:
        self.ctx.repos.candidates.replace_for_case(
            case_id, [c.to_row() for c in result.candidates]
        )
        self.ctx.repos.decisions.record(
            case_id=case_id,
            decision_type=DecisionType.SUPPLIER_SHORTLIST.value,
            recommendation=result.to_dict(),
            rationale=(
                f"Selected {len(result.selected_vendor_ids)} of {len(result.candidates)} "
                f"candidate suppliers for {result.material_code}"
            ),
            confidence=Decimal("0.8") if result.selected_vendor_ids else Decimal("0.2"),
            model_metadata={"engine": "deterministic-shortlist-v1", "weights": {k: str(v) for k, v in WEIGHTS.items()}},
            evidence=[
                {
                    "evidence_type": "VENDOR",
                    "evidence_id": c.vendor_id,
                    "role": "SUPPORTS" if c.selected else "CONTEXT",
                    "excerpt": "; ".join(c.reasons)[:500],
                    "weight": c.total_score,
                }
                for c in result.candidates[:20]
            ],
        )
        self.ctx.audit(
            entity_type="SUPPLIER_SHORTLIST",
            entity_id=case_id,
            case_id=case_id,
            action="SHORTLIST_CREATED",
            after_state={
                "selected": result.selected_vendor_ids,
                "single_source": result.is_single_source,
            },
        )
        log.info(
            "shortlist_created",
            case_id=case_id,
            material_code=result.material_code,
            candidates=len(result.candidates),
            selected=len(result.selected_vendor_ids),
            single_source=result.is_single_source,
        )


def _ensure(candidates: dict[str, Candidate], vendor_id: str, name: str = "") -> Candidate:
    candidate = candidates.get(vendor_id)
    if candidate is None:
        candidate = Candidate(vendor_id=vendor_id, vendor_name=name)
        candidates[vendor_id] = candidate
    elif name and not candidate.vendor_name:
        candidate.vendor_name = name
    return candidate


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None
