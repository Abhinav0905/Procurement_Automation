"""Stage 15 - PO recommendation and info-record maintenance.

The end of the pipeline is deliberately *not* a purchase order. It is a draft
plus a complete justification, waiting for a human to release it into SAP.

Three artifacts come out of this stage:

1. **The draft PO** - header, lines, prices, dates, account assignment, and an
   SAP-shaped payload a buyer can push or an integration can consume.
2. **The award justification** - who won, by how much, against what benchmark,
   with which deviations accepted and which risks outstanding. Written so a
   reviewer has what they need to disagree.
3. **The info-record proposal** - the negotiated price fed back into master data
   so the next requisition for this material starts from the price we just
   agreed rather than from nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from procureguard.application.bid_ranking import RankingResult
from procureguard.application.history_service import PriceBenchmark
from procureguard.domain.enums import (
    ApprovalType,
    DecisionType,
    DocumentAuthority,
    DocumentType,
    TrustState,
)
from procureguard.domain.errors import PolicyViolationError
from procureguard.domain.policies import ApprovalRequirement
from procureguard.infrastructure.factory import ServiceContext
from procureguard.infrastructure.storage.object_store import content_key
from procureguard.observability import logger

log = logger(__name__)

ZERO = Decimal(0)


@dataclass(slots=True)
class PoRecommendationResult:
    recommendation_id: str
    recommendation_number: str
    case_id: str
    vendor_id: str
    vendor_name: str
    total_amount: Decimal
    total_amount_base: Decimal
    currency: str
    savings_vs_benchmark_base: Decimal
    savings_vs_first_offer_base: Decimal
    approval_chain: list[dict[str, Any]] = field(default_factory=list)
    approval_chain_satisfied: bool = False
    missing_approvals: list[str] = field(default_factory=list)
    info_record_proposal_ids: list[str] = field(default_factory=list)
    document_version_id: str = ""
    warnings: list[str] = field(default_factory=list)
    justification: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "recommendation_id": self.recommendation_id,
            "recommendation_number": self.recommendation_number,
            "case_id": self.case_id,
            "vendor_id": self.vendor_id,
            "vendor_name": self.vendor_name,
            "total_amount": str(self.total_amount),
            "total_amount_base": str(self.total_amount_base),
            "currency": self.currency,
            "savings_vs_benchmark_base": str(self.savings_vs_benchmark_base),
            "savings_vs_first_offer_base": str(self.savings_vs_first_offer_base),
            "approval_chain": self.approval_chain,
            "approval_chain_satisfied": self.approval_chain_satisfied,
            "missing_approvals": self.missing_approvals,
            "info_record_proposal_ids": self.info_record_proposal_ids,
            "document_version_id": self.document_version_id,
            "warnings": self.warnings,
        }


class PoRecommendationService:
    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx

    def recommend(
        self,
        *,
        case_id: str,
        ranking: RankingResult,
        benchmarks: dict[str, PriceBenchmark] | None = None,
        award_vendor_id: str = "",
    ) -> PoRecommendationResult:
        case = self.ctx.repos.cases.require(case_id)
        benchmarks = benchmarks or {}

        winner = self._select_winner(ranking, award_vendor_id)
        if winner is None:
            raise PolicyViolationError(
                "No technically qualified bid is available to recommend", case_id=case_id
            )

        quotation = self.ctx.repos.quotations.get(
            winner.quotation_id, commercial_unlocked=case.commercial_unlocked
        )
        if quotation is None:
            raise PolicyViolationError(
                f"Winning quotation {winner.quotation_id} is not readable", case_id=case_id
            )

        rfq = self.ctx.repos.rfqs.latest_for_case(case_id)
        offers = self.ctx.repos.normalized_offers.list_for_quotation(quotation.id)
        pr = self.ctx.repos.requisitions.get(case.pr_number)

        deviations = self._accepted_deviations(case_id, quotation.id)
        is_single_source = len(ranking.bids) < self.ctx.policy.min_suppliers_per_rfq

        chain = self.ctx.policy.approval_chain_for_award(
            award_value_base=winner.total_base,
            is_single_source=is_single_source,
            has_deviations=bool(deviations),
        )
        approvals = self.ctx.repos.approvals.list_for_case(case_id)
        satisfied, missing = self.ctx.policy.award_chain_satisfied(chain, approvals)

        savings_vs_benchmark, savings_vs_first = self._savings(
            case_id, winner, benchmarks, ranking
        )
        justification = self._justification(
            case=case,
            winner=winner,
            ranking=ranking,
            benchmarks=benchmarks,
            deviations=deviations,
            is_single_source=is_single_source,
            savings_vs_benchmark=savings_vs_benchmark,
            savings_vs_first=savings_vs_first,
        )

        recommendation = self.ctx.repos.po_recommendations.create(
            case_id=case_id,
            recommendation_number=self.ctx.repos.po_recommendations.next_number(),
            status="RECOMMENDED",
            vendor_id=winner.vendor_id,
            vendor_name=winner.vendor_name,
            quotation_id=quotation.id,
            ranking_run_id=ranking.ranking_run_id,
            plant_code=rfq.delivery_plant if rfq else (pr.plant_code if pr else ""),
            currency=quotation.currency or self.ctx.settings.base_currency,
            incoterm=quotation.incoterm or (rfq.required_incoterm if rfq else "DAP"),
            incoterm_location=quotation.incoterm_location
            or (rfq.required_incoterm_location if rfq else ""),
            payment_terms=quotation.payment_terms or "NET 45",
            total_amount=Decimal(str(quotation.total_amount or 0)),
            total_amount_base=winner.total_base,
            benchmark_total_base=_benchmark_total(benchmarks),
            savings_vs_benchmark_base=savings_vs_benchmark,
            savings_vs_first_offer_base=savings_vs_first,
            justification=justification,
            award_rationale={
                "ranking": ranking.to_dict(),
                "deviations": deviations,
                "single_source": is_single_source,
                "negotiation": self._negotiation_summary(case_id),
            },
            approval_chain=[_chain_entry(item) for item in chain],
            approval_chain_satisfied=satisfied,
            expected_delivery_date=(
                datetime.now(UTC) + timedelta(days=int(quotation.lead_time_days or 0))
                if quotation.lead_time_days
                else None
            ),
        )

        offers_by_line = {int(o.rfq_line_number): o for o in offers}
        for line in sorted(quotation.lines, key=lambda x: x.rfq_line_number):
            rfq_line = next(
                (r for r in (rfq.lines if rfq else []) if r.line_number == line.rfq_line_number),
                None,
            )
            pr_line = next(
                (p for p in (pr.lines if pr else []) if p.line_number == line.rfq_line_number),
                None,
            )
            offer = offers_by_line.get(int(line.rfq_line_number))
            benchmark = benchmarks.get(line.material_code or (rfq_line.material_code if rfq_line else ""))
            benchmark_price = benchmark.benchmark_unit_price if benchmark else None
            unit_base = offer.unit_price_base if offer else None
            variance = (
                ((unit_base - benchmark_price) / benchmark_price * Decimal(100)).quantize(
                    Decimal("0.0001")
                )
                if benchmark_price and unit_base
                else None
            )
            self.ctx.repos.po_recommendations.add_line(
                recommendation,
                line_number=line.rfq_line_number,
                material_code=line.material_code or (rfq_line.material_code if rfq_line else ""),
                description=line.offered_description
                or (rfq_line.description if rfq_line else ""),
                quantity=Decimal(str(line.quantity or 0)),
                uom=line.uom or "EA",
                unit_price=Decimal(str(line.unit_price or 0)),
                price_unit=int(line.price_per_quantity or 1),
                currency=line.currency or quotation.currency,
                line_total=Decimal(str(line.line_total or 0)),
                delivery_date=(rfq_line.required_date if rfq_line else None),
                plant_code=(pr_line.plant_code if pr_line else recommendation.plant_code),
                storage_location=(pr_line.storage_location if pr_line else ""),
                cost_center=(pr_line.cost_center if pr_line else ""),
                gl_account=(pr_line.gl_account if pr_line else ""),
                benchmark_unit_price_base=benchmark_price,
                price_variance_pct=variance,
            )

        recommendation.sap_payload = self._sap_payload(recommendation, case, pr)
        document_version_id = self._store_document(recommendation, justification, case_id)
        recommendation.document_version_id = document_version_id

        proposals = self._propose_info_records(case_id, recommendation, quotation, benchmarks)
        self.ctx.session.flush()

        warnings: list[str] = list(ranking.warnings)
        if not satisfied:
            warnings.append(
                "The award approval chain is not yet satisfied; this recommendation cannot be "
                "released as a purchase order"
            )
        po_decision = self.ctx.policy.may_create_po_in_erp()
        if not po_decision.allowed:
            warnings.append(po_decision.reason)

        result = PoRecommendationResult(
            recommendation_id=recommendation.id,
            recommendation_number=recommendation.recommendation_number,
            case_id=case_id,
            vendor_id=winner.vendor_id,
            vendor_name=winner.vendor_name,
            total_amount=Decimal(str(recommendation.total_amount)),
            total_amount_base=winner.total_base,
            currency=recommendation.currency,
            savings_vs_benchmark_base=savings_vs_benchmark,
            savings_vs_first_offer_base=savings_vs_first,
            approval_chain=[_chain_entry(item) for item in chain],
            approval_chain_satisfied=satisfied,
            missing_approvals=list(missing),
            info_record_proposal_ids=proposals,
            document_version_id=document_version_id,
            warnings=warnings,
            justification=justification,
        )

        self.ctx.repos.decisions.record(
            case_id=case_id,
            decision_type=DecisionType.PO_RECOMMENDATION.value,
            recommendation=result.to_dict(),
            rationale=justification[:8000],
            confidence=Decimal("0.9") if satisfied else Decimal("0.6"),
            model_metadata={"engine": "deterministic-po-recommender-v1"},
            evidence=[
                {
                    "evidence_type": "QUOTATION",
                    "evidence_id": quotation.id,
                    "role": "SOURCE_OF_TRUTH",
                },
                {
                    "evidence_type": "BID_RANKING",
                    "evidence_id": ranking.ranking_run_id,
                    "role": "SUPPORTS",
                },
            ],
        )
        self.ctx.audit(
            entity_type="PO_RECOMMENDATION",
            entity_id=recommendation.id,
            case_id=case_id,
            action="PO_RECOMMENDED",
            after_state={
                "vendor_id": winner.vendor_id,
                "total_base": str(winner.total_base),
                "approval_chain_satisfied": satisfied,
            },
        )
        log.info(
            "po_recommended",
            case_id=case_id,
            vendor_id=winner.vendor_id,
            total_base=str(winner.total_base),
            chain_satisfied=satisfied,
        )
        return result

    # ---------------------------------------------------------------- release
    def release(
        self, *, recommendation_id: str, actor_id: str, erp_po_number: str = ""
    ) -> dict[str, Any]:
        """Human release. The only path from recommendation to order."""
        recommendation = self.ctx.repos.po_recommendations.get(recommendation_id)
        if recommendation is None:
            raise PolicyViolationError(f"Recommendation {recommendation_id} not found")
        if not recommendation.approval_chain_satisfied:
            raise PolicyViolationError(
                "The award approval chain is not satisfied; the PO cannot be released",
                recommendation_id=recommendation_id,
            )
        if not actor_id or actor_id.upper() in ("SYSTEM", "AGENT"):
            raise PolicyViolationError("PO release requires an authenticated human actor")

        approvals = self.ctx.repos.approvals.list_for_case(
            recommendation.case_id, ApprovalType.PO_RELEASE
        )
        if not any(a.is_positive for a in approvals):
            raise PolicyViolationError(
                "PO release requires a recorded PO_RELEASE approval",
                recommendation_id=recommendation_id,
            )

        self.ctx.repos.po_recommendations.release(
            recommendation, actor_id=actor_id, erp_po_number=erp_po_number
        )
        self.ctx.audit(
            entity_type="PO_RECOMMENDATION",
            entity_id=recommendation.id,
            case_id=recommendation.case_id,
            action="PO_RELEASED",
            actor_id=actor_id,
            after_state={"erp_po_number": erp_po_number},
            detail="Draft purchase order released for ERP creation by a human buyer",
        )
        return {
            "recommendation_id": recommendation.id,
            "status": recommendation.status,
            "erp_po_number": erp_po_number,
            "released_by": actor_id,
        }

    def apply_info_record_proposal(
        self, *, proposal_id: str, actor_id: str
    ) -> dict[str, Any]:
        """Write the negotiated price back into master data."""
        proposal = self.ctx.repos.info_record_proposals.get(proposal_id)
        if proposal is None:
            raise PolicyViolationError(f"Info record proposal {proposal_id} not found")
        if proposal.status == "APPLIED":
            return {"proposal_id": proposal_id, "status": "APPLIED", "already_applied": True}

        existing = self.ctx.repos.info_records.get_active(
            proposal.material_code, proposal.vendor_id, proposal.plant_code
        )
        created = self.ctx.repos.info_records.create(
            info_record_number=self.ctx.repos.info_records.next_number(),
            material_code=proposal.material_code,
            vendor_id=proposal.vendor_id,
            plant_code=proposal.plant_code,
            net_price=proposal.net_price,
            currency=proposal.currency,
            price_unit=proposal.price_unit,
            order_uom=proposal.order_uom,
            minimum_order_quantity=proposal.minimum_order_quantity,
            planned_delivery_days=proposal.planned_delivery_days,
            incoterm=proposal.incoterm,
            payment_terms=proposal.payment_terms,
            price_scales=proposal.price_scales,
            valid_from=proposal.valid_from,
            valid_to=proposal.valid_to,
            source_case_id=proposal.case_id,
            is_active=True,
        )
        if existing is not None:
            self.ctx.repos.info_records.supersede(existing.id, created)
        self.ctx.repos.info_record_proposals.mark_applied(
            proposal_id, actor_id=actor_id, info_record_id=created.id
        )
        self.ctx.audit(
            entity_type="INFO_RECORD",
            entity_id=created.id,
            case_id=proposal.case_id,
            action="INFO_RECORD_MAINTAINED",
            actor_id=actor_id,
            after_state={
                "material_code": proposal.material_code,
                "vendor_id": proposal.vendor_id,
                "net_price": str(proposal.net_price),
                "currency": proposal.currency,
                "superseded": existing.id if existing else None,
            },
        )
        log.info(
            "info_record_maintained",
            case_id=proposal.case_id,
            material_code=proposal.material_code,
            vendor_id=proposal.vendor_id,
        )
        return {
            "proposal_id": proposal_id,
            "info_record_id": created.id,
            "info_record_number": created.info_record_number,
            "status": "APPLIED",
            "superseded_info_record_id": existing.id if existing else "",
        }

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _select_winner(ranking: RankingResult, award_vendor_id: str) -> Any:
        if award_vendor_id:
            return next((b for b in ranking.bids if b.vendor_id == award_vendor_id), None)
        return ranking.l1

    def _accepted_deviations(self, case_id: str, quotation_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for assessment in self.ctx.repos.compliance.list_for_quotation(quotation_id):
            if not assessment.deviation_accepted and assessment.status not in ("DEVIATION",):
                continue
            requirement = self.ctx.repos.requirements.get(assessment.requirement_id)
            out.append(
                {
                    "requirement_key": requirement.requirement_key if requirement else "",
                    "attribute": requirement.attribute if requirement else "",
                    "status": assessment.status,
                    "offered_value": assessment.offered_value,
                    "accepted": bool(assessment.deviation_accepted),
                    "approval_id": assessment.deviation_approval_id,
                    "reviewer": assessment.reviewer_id,
                    "note": assessment.reviewer_note,
                }
            )
        return out

    def _savings(
        self,
        case_id: str,
        winner: Any,
        benchmarks: dict[str, PriceBenchmark],
        ranking: RankingResult,
    ) -> tuple[Decimal, Decimal]:
        benchmark_total = _benchmark_total(benchmarks)
        savings_vs_benchmark = (
            (benchmark_total - winner.total_base).quantize(Decimal("0.01"))
            if benchmark_total is not None
            else ZERO
        )
        rounds = self.ctx.repos.negotiations.list_rounds(case_id)
        first_offer = (
            Decimal(str(rounds[0].baseline_total_base)) if rounds else winner.total_base
        )
        savings_vs_first = (first_offer - winner.total_base).quantize(Decimal("0.01"))
        return savings_vs_benchmark, savings_vs_first

    def _negotiation_summary(self, case_id: str) -> list[dict[str, Any]]:
        from procureguard.application.negotiation import NegotiationService

        return NegotiationService(self.ctx).history(case_id)

    def _justification(
        self,
        *,
        case: Any,
        winner: Any,
        ranking: RankingResult,
        benchmarks: dict[str, PriceBenchmark],
        deviations: list[dict[str, Any]],
        is_single_source: bool,
        savings_vs_benchmark: Decimal,
        savings_vs_first: Decimal,
    ) -> str:
        currency = ranking.base_currency
        parts = [
            f"# Award justification - {case.case_id}",
            "",
            f"**Recommended supplier:** {winner.vendor_name} ({winner.vendor_id})",
            f"**Award value:** {winner.total_base} {currency} (total cost of ownership basis)",
            f"**Position:** {winner.position_label} of {len(ranking.bids)} qualified bids",
            "",
            "## Why this supplier",
            "",
        ]
        if winner.position == 1:
            parts.append(
                f"{winner.vendor_name} submitted the lowest evaluated total cost of ownership "
                f"and met every mandatory technical requirement."
            )
        else:
            parts.append(
                f"{winner.vendor_name} is {winner.delta_vs_l1_pct}% above the lowest bid "
                f"({winner.delta_vs_l1_base} {currency}), recommended on a weighted value "
                f"basis with a technical score of {winner.technical_score}."
            )
        if len(ranking.bids) > 1:
            runner_up = ranking.bids[1] if winner.position == 1 else ranking.bids[0]
            parts.append(
                f"The nearest alternative is {runner_up.vendor_name} ({runner_up.vendor_id}) at "
                f"{runner_up.total_base} {currency}, technical score {runner_up.technical_score}."
            )

        parts += ["", "## Competition", ""]
        if is_single_source:
            parts.append(
                f"Only {len(ranking.bids)} qualified bid(s) were received against a competitive "
                f"minimum of {self.ctx.policy.min_suppliers_per_rfq}. This award requires a "
                f"single/limited-source justification."
            )
        else:
            parts.append(
                f"{len(ranking.bids)} qualified bids were evaluated"
                + (
                    f", with {len(ranking.disqualified)} further bid(s) technically disqualified."
                    if ranking.disqualified
                    else "."
                )
            )

        parts += ["", "## Price validation", ""]
        benchmark_total = _benchmark_total(benchmarks)
        if benchmark_total is not None:
            direction = "below" if savings_vs_benchmark > 0 else "above"
            parts.append(
                f"The award value is {abs(savings_vs_benchmark)} {currency} {direction} the "
                f"historical benchmark of {benchmark_total} {currency}."
            )
        else:
            parts.append(
                "No internal purchase history exists for this material, so the price could not "
                "be benchmarked against prior spend. Competition is the only price validation."
            )
        if savings_vs_first > 0:
            parts.append(
                f"Negotiation reduced the offer by {savings_vs_first} {currency} against the "
                f"first-round position."
            )
        if winner.delta_vs_benchmark_pct is not None:
            parts.append(
                f"Weighted price variance against history: {winner.delta_vs_benchmark_pct}%."
            )

        if deviations:
            parts += ["", "## Technical deviations", ""]
            for deviation in deviations:
                state = "ACCEPTED" if deviation["accepted"] else "NOT YET ACCEPTED"
                parts.append(
                    f"- {deviation['requirement_key']} {deviation['attribute']}: offered "
                    f"{deviation['offered_value'][:120]!r} - {state}"
                    + (f" by {deviation['reviewer']}" if deviation["reviewer"] else "")
                )

        if ranking.split_award.get("beneficial"):
            parts += [
                "",
                "## Alternative considered",
                "",
                f"A line-by-line split award across "
                f"{ranking.split_award['supplier_count']} suppliers would cost "
                f"{ranking.split_award['split_total_base']} {currency}, saving "
                f"{ranking.split_award['saving_pct']}%. Single-source award is recommended for "
                f"supply-chain simplicity; override this if the saving justifies the extra "
                f"supplier relationship.",
            ]

        outstanding = [w for w in ranking.warnings if w]
        if outstanding:
            parts += ["", "## Outstanding risks", ""]
            parts += [f"- {warning}" for warning in outstanding]

        parts += [
            "",
            "---",
            "",
            "*Prepared by ProcureGuard from the evaluated bid tabulation. All figures are "
            "reproducible from the stored normalisation records. This is a recommendation; "
            "the purchase order is created only on human release.*",
        ]
        return "\n".join(parts)

    def _sap_payload(self, recommendation: Any, case: Any, pr: Any) -> dict[str, Any]:
        """SAP-shaped payload for the ERP integration or a manual keyer."""
        return {
            "PurchaseOrder": {
                "CompanyCode": self.ctx.settings.default_company_code,
                "DocumentType": recommendation.document_type,
                "Supplier": recommendation.vendor_id,
                "PurchasingOrganization": recommendation.purchasing_org,
                "PurchasingGroup": recommendation.purchasing_group,
                "DocumentCurrency": recommendation.currency,
                "IncotermsClassification": recommendation.incoterm,
                "IncotermsLocation1": recommendation.incoterm_location,
                "PaymentTerms": recommendation.payment_terms,
                "PurchaseRequisition": case.pr_number,
                "YourReference": recommendation.recommendation_number,
            },
            "PurchaseOrderItems": [
                {
                    "PurchaseOrderItem": f"{line.line_number * 10:05d}",
                    "Material": line.material_code,
                    "PurchaseOrderItemText": line.description[:40],
                    "Plant": line.plant_code,
                    "StorageLocation": line.storage_location,
                    "OrderQuantity": str(line.quantity),
                    "PurchaseOrderQuantityUnit": line.uom,
                    "NetPriceAmount": str(line.unit_price),
                    "NetPriceQuantity": line.price_unit,
                    "DocumentCurrency": line.currency,
                    "TaxCode": line.tax_code,
                    "AccountAssignmentCategory": "K" if line.cost_center else "",
                    "CostCenter": line.cost_center,
                    "GLAccount": line.gl_account,
                    "ScheduleLines": [
                        {
                            "ScheduleLineDeliveryDate": (
                                line.delivery_date.date().isoformat()
                                if line.delivery_date
                                else None
                            ),
                            "ScheduleLineOrderQuantity": str(line.quantity),
                        }
                    ],
                }
                for line in sorted(recommendation.lines, key=lambda x: x.line_number)
            ],
            "_meta": {
                "generated_by": "ProcureGuard",
                "case_id": recommendation.case_id,
                "recommendation_number": recommendation.recommendation_number,
                "requires_human_release": not self.ctx.settings.allow_automated_po_creation,
            },
        }

    def _store_document(self, recommendation: Any, justification: str, case_id: str) -> str:
        content = justification.encode("utf-8")
        stored = self.ctx.object_store.put(
            key=content_key(prefix="award", content=content, extension=".md"),
            body=content,
            content_type="text/markdown; charset=utf-8",
            metadata={"case_id": case_id},
        )
        document = self.ctx.repos.documents.get_or_create_document(
            logical_name=f"{recommendation.recommendation_number}-award.md",
            document_type=DocumentType.PURCHASE_ORDER,
            case_id=case_id,
        )
        version, _ = self.ctx.repos.documents.add_version(
            document,
            content=content,
            storage_uri=stored.uri,
            media_type="text/markdown; charset=utf-8",
            original_filename=f"{recommendation.recommendation_number}-award.md",
            authority=DocumentAuthority.PROCUREMENT,
            trust_state=TrustState.AUTHORITATIVE,
            uploaded_by=self.ctx.actor_id,
        )
        return version.id

    def _propose_info_records(
        self,
        case_id: str,
        recommendation: Any,
        quotation: Any,
        benchmarks: dict[str, PriceBenchmark],
    ) -> list[str]:
        """One proposal per awarded material, with the price-change delta."""
        proposal_ids: list[str] = []
        for line in recommendation.lines:
            if not line.material_code:
                continue
            existing = self.ctx.repos.info_records.get_active(
                line.material_code, recommendation.vendor_id, recommendation.plant_code
            )
            previous_price = Decimal(str(existing.net_price)) if existing else None
            change_pct = (
                ((Decimal(str(line.unit_price)) - previous_price) / previous_price * Decimal(100)).quantize(
                    Decimal("0.0001")
                )
                if previous_price and previous_price > 0
                else None
            )
            proposal = self.ctx.repos.info_record_proposals.create(
                case_id=case_id,
                material_code=line.material_code,
                vendor_id=recommendation.vendor_id,
                plant_code=recommendation.plant_code,
                action="UPDATE" if existing else "CREATE",
                existing_info_record_id=existing.id if existing else "",
                net_price=Decimal(str(line.unit_price)),
                currency=line.currency,
                price_unit=int(line.price_unit or 1),
                order_uom=line.uom,
                minimum_order_quantity=Decimal(str(quotation.minimum_order_quantity or 1)),
                planned_delivery_days=int(quotation.lead_time_days or 0),
                incoterm=recommendation.incoterm,
                payment_terms=recommendation.payment_terms,
                price_scales=[],
                valid_from=datetime.now(UTC),
                valid_to=(
                    quotation.valid_until
                    or datetime.now(UTC) + timedelta(days=365)
                ),
                previous_net_price=previous_price,
                price_change_pct=change_pct,
                status="PROPOSED",
            )
            proposal_ids.append(proposal.id)
        return proposal_ids


def _benchmark_total(benchmarks: dict[str, PriceBenchmark]) -> Decimal | None:
    totals = [
        b.extended_benchmark() for b in benchmarks.values() if b.extended_benchmark() is not None
    ]
    if not totals:
        return None
    return sum(totals, ZERO).quantize(Decimal("0.01"))


def _chain_entry(item: ApprovalRequirement) -> dict[str, Any]:
    return {
        "approval_type": str(item.approval_type),
        "eligible_roles": [str(r) for r in item.eligible_roles],
        "minimum_approvers": item.minimum_approvers,
        "reason": item.reason,
    }
