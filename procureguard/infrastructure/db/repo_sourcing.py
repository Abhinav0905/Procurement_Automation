"""Sourcing repositories: requisitions through PO recommendation.

The sealed-bid rule is enforced here rather than in a service: quotation reads
go through `QuotationRepository`, which strips commercial fields unless the
caller presents an unlocked case. A service that forgets to check therefore
still cannot leak a price.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import asc, desc, func, select

from procureguard.domain.entities import PurchaseRequisition, PurchaseRequisitionLine
from procureguard.domain.enums import QuotationStatus, RfqInvitationStatus, RfqStatus
from procureguard.domain.errors import NotFoundError, SealedBidError
from procureguard.observability import logger

from .models import (
    BidRankingModel,
    CommunicationModel,
    ComplianceAssessmentModel,
    InfoRecordProposalModel,
    NegotiationRoundModel,
    NegotiationTargetModel,
    NormalizedOfferModel,
    PoRecommendationLineModel,
    PoRecommendationModel,
    PurchaseRequisitionLineModel,
    PurchaseRequisitionModel,
    QuotationLineModel,
    QuotationModel,
    RequirementModel,
    RfqInvitationModel,
    RfqLineModel,
    RfqModel,
    SupplierCandidateModel,
    utcnow,
)
from .repo_core import TenantScopedRepository

log = logger(__name__)

# Fields hidden while a case is technically sealed.
SEALED_FIELDS = (
    "currency",
    "total_amount",
    "freight_amount",
    "packing_amount",
    "tooling_amount",
    "other_charges",
    "discount_amount",
    "payment_terms",
)


class SqlRequisitionRepository(TenantScopedRepository):
    def save(self, pr: PurchaseRequisition, **header_extra: Any) -> PurchaseRequisitionModel:
        row = self.get_model(pr.pr_number)
        if row is None:
            row = PurchaseRequisitionModel(
                tenant_id=self.tenant_id, pr_number=pr.pr_number, plant_code=pr.plant_code,
                requester=pr.requester,
            )
            self.session.add(row)
            self.session.flush()
        row.plant_code = pr.plant_code
        row.requester = pr.requester
        row.requester_email = pr.requester_email
        row.department = pr.department
        row.company_code = pr.company_code or row.company_code
        row.priority = pr.priority.upper()
        row.currency = pr.currency or row.currency
        row.justification = pr.justification
        row.budget_code = pr.budget_code
        row.source_channel = pr.source_channel
        for key, value in header_extra.items():
            if hasattr(row, key):
                setattr(row, key, value)

        existing = {line.line_number: line for line in row.lines}
        for line in pr.lines:
            model = existing.get(line.line_number)
            if model is None:
                model = PurchaseRequisitionLineModel(
                    tenant_id=self.tenant_id,
                    requisition_id=row.id,
                    line_number=line.line_number,
                    quantity=line.quantity,
                    uom=line.uom,
                )
                self.session.add(model)
            model.material_code = line.material_code
            model.description = line.description
            model.quantity = Decimal(str(line.quantity))
            model.uom = line.uom
            model.required_date = line.required_date
            model.plant_code = line.plant_code or pr.plant_code
            model.storage_location = line.storage_location
            model.cost_center = line.cost_center
            model.gl_account = line.gl_account
            model.estimated_unit_price = line.estimated_unit_price
            model.currency = line.currency
            model.specification_reference = line.specification_reference
            model.manufacturer_part_number = line.manufacturer_part_number
            model.preferred_vendor_id = line.preferred_vendor_id
            model.free_text_only = line.free_text_only
            model.notes = line.notes
        self.session.flush()
        return row

    def get_model(self, pr_number: str) -> PurchaseRequisitionModel | None:
        return self.session.scalars(
            self._scoped(select(PurchaseRequisitionModel), PurchaseRequisitionModel).where(
                PurchaseRequisitionModel.pr_number == pr_number
            )
        ).first()

    def get(self, pr_number: str) -> PurchaseRequisition | None:
        row = self.get_model(pr_number)
        if row is None:
            return None
        return PurchaseRequisition(
            pr_number=row.pr_number,
            plant_code=row.plant_code,
            requester=row.requester,
            requester_email=row.requester_email,
            department=row.department,
            company_code=row.company_code,
            currency=row.currency,
            priority=row.priority,
            justification=row.justification,
            budget_code=row.budget_code,
            created_at=row.created_at,
            source_channel=row.source_channel,
            lines=[
                PurchaseRequisitionLine(
                    line_number=line.line_number,
                    material_code=line.material_code,
                    quantity=Decimal(str(line.quantity)),
                    uom=line.uom,
                    description=line.description,
                    required_date=line.required_date,
                    plant_code=line.plant_code,
                    storage_location=line.storage_location,
                    cost_center=line.cost_center,
                    gl_account=line.gl_account,
                    estimated_unit_price=(
                        Decimal(str(line.estimated_unit_price))
                        if line.estimated_unit_price is not None
                        else None
                    ),
                    currency=line.currency,
                    specification_reference=line.specification_reference,
                    manufacturer_part_number=line.manufacturer_part_number,
                    preferred_vendor_id=line.preferred_vendor_id,
                    free_text_only=bool(line.free_text_only),
                    notes=line.notes,
                )
                for line in sorted(row.lines, key=lambda x: x.line_number)
            ],
        )

    def update_line_validation(
        self,
        pr_number: str,
        line_number: int,
        *,
        status: str,
        messages: Sequence[str],
        resolved_material_code: str = "",
        resolution_method: str = "",
        resolution_confidence: Decimal | float = 0,
        normalized_uom: str = "",
    ) -> None:
        row = self.get_model(pr_number)
        if row is None:
            raise NotFoundError(f"Requisition {pr_number} not found")
        for line in row.lines:
            if line.line_number != line_number:
                continue
            line.validation_status = status
            line.validation_messages = list(messages)
            line.resolved_material_code = resolved_material_code
            line.resolution_method = resolution_method
            line.resolution_confidence = Decimal(str(resolution_confidence))
            if normalized_uom:
                line.normalized_uom = normalized_uom
        self.session.flush()


class SqlRequirementRepository(TenantScopedRepository):
    def replace_for_case(
        self, case_id: str, requirements: Sequence[dict[str, Any]]
    ) -> list[RequirementModel]:
        """Deactivate superseded requirements rather than deleting them.

        A quotation already assessed against requirement R must still resolve R
        when the audit trail is replayed.
        """
        for row in self.list_active(case_id):
            row.active = False
        self.session.flush()

        created: list[RequirementModel] = []
        for item in requirements:
            row = RequirementModel(
                tenant_id=self.tenant_id,
                case_id=case_id,
                pr_line_number=int(item.get("pr_line_number", 1)),
                requirement_key=str(item["requirement_key"])[:80],
                kind=str(item["kind"]),
                obligation=str(item["obligation"]),
                attribute=str(item["attribute"])[:255],
                operator=str(item["operator"]),
                raw_text=str(item.get("raw_text", "")),
                target_value=str(item.get("target_value", "")),
                target_numeric=_opt_dec(item.get("target_numeric")),
                lower_numeric=_opt_dec(item.get("lower_numeric")),
                upper_numeric=_opt_dec(item.get("upper_numeric")),
                tolerance_plus=_opt_dec(item.get("tolerance_plus")),
                tolerance_minus=_opt_dec(item.get("tolerance_minus")),
                uom=str(item.get("uom", ""))[:16],
                allowed_values=list(item.get("allowed_values", [])),
                weight=Decimal(str(item.get("weight", 1))),
                source_document_version_id=str(item.get("source_document_version_id", "")),
                source_location=str(item.get("source_location", ""))[:255],
                trust_state=str(item.get("trust_state", "UNVERIFIED")),
                extraction_confidence=Decimal(str(item.get("extraction_confidence", 0))),
                active=True,
            )
            self.session.add(row)
            created.append(row)
        self.session.flush()
        return created

    def list_active(self, case_id: str) -> list[RequirementModel]:
        return list(
            self.session.scalars(
                self._scoped(select(RequirementModel), RequirementModel)
                .where(RequirementModel.case_id == case_id, RequirementModel.active.is_(True))
                .order_by(RequirementModel.obligation, RequirementModel.requirement_key)
            ).all()
        )

    def get(self, requirement_id: str) -> RequirementModel | None:
        row = self.session.get(RequirementModel, requirement_id)
        return row if row and row.tenant_id == self.tenant_id else None

    def mark_reviewed(self, requirement_id: str, reviewer_id: str) -> None:
        row = self.get(requirement_id)
        if row is None:
            return
        row.reviewed_by = reviewer_id
        row.reviewed_at = utcnow()
        row.trust_state = "VERIFIED"
        self.session.flush()


class SqlSupplierCandidateRepository(TenantScopedRepository):
    def replace_for_case(
        self, case_id: str, candidates: Sequence[dict[str, Any]]
    ) -> list[SupplierCandidateModel]:
        existing = {row.vendor_id: row for row in self.list_for_case(case_id)}
        kept: list[SupplierCandidateModel] = []
        for item in candidates:
            vendor_id = str(item["vendor_id"])
            row = existing.pop(vendor_id, None)
            if row is None:
                row = SupplierCandidateModel(
                    tenant_id=self.tenant_id, case_id=case_id, vendor_id=vendor_id
                )
                self.session.add(row)
            row.vendor_name = str(item.get("vendor_name", ""))[:255]
            row.rank = int(item.get("rank", 0))
            row.total_score = Decimal(str(item.get("total_score", 0)))
            row.history_score = Decimal(str(item.get("history_score", 0)))
            row.performance_score = Decimal(str(item.get("performance_score", 0)))
            row.capability_score = Decimal(str(item.get("capability_score", 0)))
            row.commercial_score = Decimal(str(item.get("commercial_score", 0)))
            row.risk_score = Decimal(str(item.get("risk_score", 0)))
            row.responsiveness_score = Decimal(str(item.get("responsiveness_score", 0)))
            row.similarity_score = Decimal(str(item.get("similarity_score", 0)))
            row.score_breakdown = dict(item.get("score_breakdown", {}))
            row.rationale = str(item.get("rationale", ""))
            row.selection_source = str(item.get("selection_source", "SCORED"))
            row.selected = bool(item.get("selected", False))
            row.excluded_reason = str(item.get("excluded_reason", ""))
            row.last_purchase_date = item.get("last_purchase_date")
            row.last_unit_price_base = _opt_dec(item.get("last_unit_price_base"))
            row.purchase_count_36m = int(item.get("purchase_count_36m", 0))
            kept.append(row)
        for orphan in existing.values():
            if orphan.added_by == "AGENT":
                self.session.delete(orphan)
        self.session.flush()
        return kept

    def list_for_case(self, case_id: str, *, selected_only: bool = False) -> list[SupplierCandidateModel]:
        stmt = self._scoped(select(SupplierCandidateModel), SupplierCandidateModel).where(
            SupplierCandidateModel.case_id == case_id
        )
        if selected_only:
            stmt = stmt.where(SupplierCandidateModel.selected.is_(True))
        return list(self.session.scalars(stmt.order_by(SupplierCandidateModel.rank)).all())

    def set_selection(self, case_id: str, vendor_ids: Sequence[str], *, actor_id: str) -> int:
        wanted = set(vendor_ids)
        changed = 0
        for row in self.list_for_case(case_id):
            should_select = row.vendor_id in wanted
            if row.selected != should_select:
                row.selected = should_select
                row.added_by = actor_id
                changed += 1
        self.session.flush()
        return changed

    def add_manual(
        self, case_id: str, *, vendor_id: str, vendor_name: str, actor_id: str, rationale: str
    ) -> SupplierCandidateModel:
        row = self.session.scalars(
            self._scoped(select(SupplierCandidateModel), SupplierCandidateModel).where(
                SupplierCandidateModel.case_id == case_id,
                SupplierCandidateModel.vendor_id == vendor_id,
            )
        ).first()
        if row is None:
            row = SupplierCandidateModel(
                tenant_id=self.tenant_id, case_id=case_id, vendor_id=vendor_id
            )
            self.session.add(row)
        row.vendor_name = vendor_name
        row.selected = True
        row.selection_source = "MANUAL"
        row.added_by = actor_id
        row.rationale = rationale
        self.session.flush()
        return row


class SqlRfqRepository(TenantScopedRepository):
    def create(self, **fields: Any) -> RfqModel:
        row = RfqModel(tenant_id=self.tenant_id, **fields)
        self.session.add(row)
        self.session.flush()
        return row

    def get(self, rfq_id: str) -> RfqModel | None:
        row = self.session.get(RfqModel, rfq_id)
        return row if row and row.tenant_id == self.tenant_id else None

    def get_by_number(self, rfq_number: str) -> RfqModel | None:
        return self.session.scalars(
            self._scoped(select(RfqModel), RfqModel).where(RfqModel.rfq_number == rfq_number)
        ).first()

    def latest_for_case(self, case_id: str) -> RfqModel | None:
        return self.session.scalars(
            self._scoped(select(RfqModel), RfqModel)
            .where(RfqModel.case_id == case_id)
            .order_by(desc(RfqModel.revision))
            .limit(1)
        ).first()

    def add_line(self, rfq: RfqModel, **fields: Any) -> RfqLineModel:
        row = RfqLineModel(tenant_id=self.tenant_id, rfq_id=rfq.id, **fields)
        self.session.add(row)
        self.session.flush()
        return row

    def add_invitation(self, rfq: RfqModel, **fields: Any) -> RfqInvitationModel:
        row = RfqInvitationModel(
            tenant_id=self.tenant_id, rfq_id=rfq.id, case_id=rfq.case_id, **fields
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get_invitation(self, invitation_id: str) -> RfqInvitationModel | None:
        row = self.session.get(RfqInvitationModel, invitation_id)
        return row if row and row.tenant_id == self.tenant_id else None

    def find_invitation_by_token(self, token: str) -> RfqInvitationModel | None:
        return self.session.scalars(
            self._scoped(select(RfqInvitationModel), RfqInvitationModel).where(
                RfqInvitationModel.response_token == token
            )
        ).first()

    def find_invitation(self, case_id: str, vendor_id: str) -> RfqInvitationModel | None:
        return self.session.scalars(
            self._scoped(select(RfqInvitationModel), RfqInvitationModel)
            .where(
                RfqInvitationModel.case_id == case_id,
                RfqInvitationModel.vendor_id == vendor_id,
            )
            .order_by(desc(RfqInvitationModel.created_at))
            .limit(1)
        ).first()

    def list_invitations(
        self, rfq_id: str, *, statuses: Sequence[RfqInvitationStatus] | None = None
    ) -> list[RfqInvitationModel]:
        stmt = self._scoped(select(RfqInvitationModel), RfqInvitationModel).where(
            RfqInvitationModel.rfq_id == rfq_id
        )
        if statuses:
            stmt = stmt.where(RfqInvitationModel.status.in_([s.value for s in statuses]))
        return list(self.session.scalars(stmt.order_by(RfqInvitationModel.vendor_id)).all())

    def pending_invitations(self, rfq_id: str) -> list[RfqInvitationModel]:
        return self.list_invitations(
            rfq_id,
            statuses=(
                RfqInvitationStatus.SENT,
                RfqInvitationStatus.ACKNOWLEDGED,
                RfqInvitationStatus.QUEUED,
            ),
        )

    def release(self, rfq: RfqModel, *, actor_id: str) -> None:
        rfq.status = RfqStatus.ISSUED.value
        rfq.released_by = actor_id
        rfq.released_at = utcnow()
        rfq.issue_date = rfq.issue_date or rfq.released_at
        self.session.flush()

    def close(self, rfq: RfqModel) -> None:
        rfq.status = RfqStatus.CLOSED.value
        for invitation in self.list_invitations(rfq.id):
            if invitation.status in (
                RfqInvitationStatus.SENT.value,
                RfqInvitationStatus.ACKNOWLEDGED.value,
                RfqInvitationStatus.QUEUED.value,
            ):
                invitation.status = RfqInvitationStatus.NO_RESPONSE.value
        self.session.flush()

    def next_number(self, prefix: str = "RFQ") -> str:
        count = int(
            self.session.scalar(
                self._scoped(select(func.count()).select_from(RfqModel), RfqModel)
            )
            or 0
        )
        return f"{prefix}-{utcnow().year}-{count + 1:06d}"


class SqlQuotationRepository(TenantScopedRepository):
    """Quotation access, with the sealed-bid gate baked in."""

    def create(self, **fields: Any) -> QuotationModel:
        row = QuotationModel(tenant_id=self.tenant_id, **fields)
        self.session.add(row)
        self.session.flush()
        return row

    def get(self, quotation_id: str, *, commercial_unlocked: bool = False) -> QuotationModel | None:
        row = self.session.get(QuotationModel, quotation_id)
        if row is None or row.tenant_id != self.tenant_id:
            return None
        if row.is_sealed and not commercial_unlocked:
            raise SealedBidError(
                "Commercial data for this quotation is sealed until technical approval",
                quotation_id=quotation_id,
            )
        return row

    def get_metadata_only(self, quotation_id: str) -> QuotationModel | None:
        """Header access that never touches sealed commercial fields."""
        row = self.session.get(QuotationModel, quotation_id)
        return row if row and row.tenant_id == self.tenant_id else None

    def list_for_case(
        self,
        case_id: str,
        *,
        commercial_unlocked: bool = False,
        latest_only: bool = True,
        negotiation_round: int | None = None,
    ) -> list[QuotationModel]:
        stmt = self._scoped(select(QuotationModel), QuotationModel).where(
            QuotationModel.case_id == case_id,
            QuotationModel.status != QuotationStatus.SUPERSEDED.value,
            QuotationModel.status != QuotationStatus.WITHDRAWN.value,
        )
        if negotiation_round is not None:
            stmt = stmt.where(QuotationModel.negotiation_round == negotiation_round)
        # Latest means highest negotiation round, then highest revision within
        # it. Ordering on revision alone is ambiguous once a negotiation round
        # exists, because a round-1 response starts again at revision 1.
        rows = list(
            self.session.scalars(
                stmt.order_by(
                    QuotationModel.vendor_id,
                    desc(QuotationModel.negotiation_round),
                    desc(QuotationModel.revision),
                )
            ).all()
        )
        if latest_only:
            seen: set[str] = set()
            latest: list[QuotationModel] = []
            for row in rows:
                if row.vendor_id in seen:
                    continue
                seen.add(row.vendor_id)
                latest.append(row)
            rows = latest
        # No redaction pass here, deliberately. A sealed quotation's plaintext
        # columns contain no prices at all - the commercial payload lives only in
        # the encrypted envelope until a human unseals it - so there is nothing
        # to strip. An earlier version blanked these fields defensively and, by
        # assigning `row.lines = []` on a delete-orphan relationship, silently
        # deleted the quotation lines at the next flush.
        return rows

    def count_received(self, case_id: str) -> int:
        return int(
            self.session.scalar(
                self._scoped(select(func.count()).select_from(QuotationModel), QuotationModel).where(
                    QuotationModel.case_id == case_id,
                    QuotationModel.negotiation_round == 0,
                    QuotationModel.status.notin_(
                        [
                            QuotationStatus.SUPERSEDED.value,
                            QuotationStatus.WITHDRAWN.value,
                            QuotationStatus.QUARANTINED.value,
                        ]
                    ),
                )
            )
            or 0
        )

    def find_by_vendor(
        self, case_id: str, vendor_id: str, *, negotiation_round: int | None = None
    ) -> QuotationModel | None:
        stmt = self._scoped(select(QuotationModel), QuotationModel).where(
            QuotationModel.case_id == case_id, QuotationModel.vendor_id == vendor_id
        )
        if negotiation_round is not None:
            stmt = stmt.where(QuotationModel.negotiation_round == negotiation_round)
        return self.session.scalars(stmt.order_by(desc(QuotationModel.revision)).limit(1)).first()

    def add_line(self, quotation: QuotationModel, **fields: Any) -> QuotationLineModel:
        row = QuotationLineModel(tenant_id=self.tenant_id, quotation_id=quotation.id, **fields)
        self.session.add(row)
        self.session.flush()
        return row

    def supersede(self, old: QuotationModel, new: QuotationModel) -> None:
        old.status = QuotationStatus.SUPERSEDED.value
        new.supersedes_quotation_id = old.id
        new.revision = old.revision + 1
        self.session.flush()

    def unseal_all(self, case_id: str, *, actor_id: str) -> int:
        """Decrypt-in-place marker. The payload itself is decrypted by the
        crypto adapter; this records who opened the envelope and when."""
        rows = list(
            self.session.scalars(
                self._scoped(select(QuotationModel), QuotationModel).where(
                    QuotationModel.case_id == case_id, QuotationModel.is_sealed.is_(True)
                )
            ).all()
        )
        stamp = utcnow()
        for row in rows:
            row.is_sealed = False
            row.unsealed_at = stamp
            row.unsealed_by = actor_id
        self.session.flush()
        return len(rows)

    def set_technical_result(
        self,
        quotation_id: str,
        *,
        qualified: bool,
        score: Decimal | float,
        disqualification_reasons: Sequence[str] = (),
    ) -> None:
        row = self.get_metadata_only(quotation_id)
        if row is None:
            return
        row.technically_qualified = qualified
        row.technical_score = Decimal(str(score))
        row.disqualification_reasons = list(disqualification_reasons)
        row.status = (
            QuotationStatus.TECHNICALLY_QUALIFIED.value
            if qualified
            else QuotationStatus.TECHNICALLY_DISQUALIFIED.value
        )
        self.session.flush()


def commercial_projection(row: QuotationModel, *, unlocked: bool) -> dict[str, Any]:
    """Read-only view of a quotation's commercial fields.

    Returns empty values while sealed. This is a projection, never a mutation of
    the persistent instance - which is the mistake that made an earlier
    "defensive" redaction destructive.
    """
    if row.is_sealed and not unlocked:
        return dict.fromkeys(SEALED_FIELDS) | {"sealed": True, "lines": []}
    return {
        "sealed": False,
        "currency": row.currency,
        "total_amount": row.total_amount,
        "freight_amount": row.freight_amount,
        "packing_amount": row.packing_amount,
        "tooling_amount": row.tooling_amount,
        "other_charges": row.other_charges,
        "discount_amount": row.discount_amount,
        "payment_terms": row.payment_terms,
        "lines": list(row.lines),
    }


class SqlComplianceRepository(TenantScopedRepository):
    def upsert(
        self,
        *,
        case_id: str,
        quotation_id: str,
        requirement_id: str,
        vendor_id: str,
        status: str,
        offered_value: str,
        rationale: str,
        offered_numeric: Decimal | None = None,
        offered_uom: str = "",
        evidence_ids: Sequence[str] = (),
        confidence: Decimal | float = 0,
        assessed_by: str = "AGENT",
    ) -> ComplianceAssessmentModel:
        row = self.session.scalars(
            select(ComplianceAssessmentModel).where(
                ComplianceAssessmentModel.quotation_id == quotation_id,
                ComplianceAssessmentModel.requirement_id == requirement_id,
            )
        ).first()
        if row is None:
            row = ComplianceAssessmentModel(
                tenant_id=self.tenant_id,
                case_id=case_id,
                quotation_id=quotation_id,
                requirement_id=requirement_id,
                vendor_id=vendor_id,
            )
            self.session.add(row)
        row.status = status
        row.offered_value = offered_value[:8000]
        row.offered_numeric = offered_numeric
        row.offered_uom = offered_uom[:16]
        row.rationale = rationale[:8000]
        row.evidence_ids = list(evidence_ids)
        row.confidence = Decimal(str(confidence))
        row.assessed_by = assessed_by
        self.session.flush()
        return row

    def list_for_case(self, case_id: str) -> list[ComplianceAssessmentModel]:
        return list(
            self.session.scalars(
                self._scoped(select(ComplianceAssessmentModel), ComplianceAssessmentModel).where(
                    ComplianceAssessmentModel.case_id == case_id
                )
            ).all()
        )

    def list_for_quotation(self, quotation_id: str) -> list[ComplianceAssessmentModel]:
        return list(
            self.session.scalars(
                select(ComplianceAssessmentModel).where(
                    ComplianceAssessmentModel.quotation_id == quotation_id
                )
            ).all()
        )

    def accept_deviation(
        self, assessment_id: str, *, approval_id: str, reviewer_id: str, note: str
    ) -> ComplianceAssessmentModel | None:
        row = self.session.get(ComplianceAssessmentModel, assessment_id)
        if row is None or row.tenant_id != self.tenant_id:
            return None
        row.deviation_accepted = True
        row.deviation_approval_id = approval_id
        row.reviewer_id = reviewer_id
        row.reviewer_note = note[:8000]
        self.session.flush()
        return row

    def override(
        self, assessment_id: str, *, status: str, reviewer_id: str, note: str
    ) -> ComplianceAssessmentModel | None:
        row = self.session.get(ComplianceAssessmentModel, assessment_id)
        if row is None or row.tenant_id != self.tenant_id:
            return None
        row.reviewer_override_status = status
        row.reviewer_id = reviewer_id
        row.reviewer_note = note[:8000]
        row.status = status
        self.session.flush()
        return row


class SqlNormalizedOfferRepository(TenantScopedRepository):
    def replace_for_round(
        self, case_id: str, negotiation_round: int, offers: Sequence[dict[str, Any]]
    ) -> list[NormalizedOfferModel]:
        for row in self.list_for_round(case_id, negotiation_round):
            self.session.delete(row)
        self.session.flush()
        created = []
        for offer in offers:
            row = NormalizedOfferModel(
                tenant_id=self.tenant_id,
                case_id=case_id,
                negotiation_round=negotiation_round,
                **offer,
            )
            self.session.add(row)
            created.append(row)
        self.session.flush()
        return created

    def list_for_round(self, case_id: str, negotiation_round: int) -> list[NormalizedOfferModel]:
        return list(
            self.session.scalars(
                self._scoped(select(NormalizedOfferModel), NormalizedOfferModel).where(
                    NormalizedOfferModel.case_id == case_id,
                    NormalizedOfferModel.negotiation_round == negotiation_round,
                )
            ).all()
        )

    def list_for_quotation(self, quotation_id: str) -> list[NormalizedOfferModel]:
        return list(
            self.session.scalars(
                select(NormalizedOfferModel).where(
                    NormalizedOfferModel.quotation_id == quotation_id
                )
            ).all()
        )


class SqlBidRankingRepository(TenantScopedRepository):
    def save_run(
        self, case_id: str, ranking_run_id: str, rows: Sequence[dict[str, Any]]
    ) -> list[BidRankingModel]:
        created = []
        for item in rows:
            row = BidRankingModel(
                tenant_id=self.tenant_id,
                case_id=case_id,
                ranking_run_id=ranking_run_id,
                **item,
            )
            self.session.add(row)
            created.append(row)
        self.session.flush()
        return created

    def latest_run(self, case_id: str) -> list[BidRankingModel]:
        latest = self.session.scalars(
            self._scoped(select(BidRankingModel), BidRankingModel)
            .where(BidRankingModel.case_id == case_id)
            .order_by(desc(BidRankingModel.created_at))
            .limit(1)
        ).first()
        if latest is None:
            return []
        return self.list_run(latest.ranking_run_id)

    def list_run(self, ranking_run_id: str) -> list[BidRankingModel]:
        return list(
            self.session.scalars(
                self._scoped(select(BidRankingModel), BidRankingModel)
                .where(BidRankingModel.ranking_run_id == ranking_run_id)
                .order_by(asc(BidRankingModel.position))
            ).all()
        )

    def l1(self, case_id: str) -> BidRankingModel | None:
        rows = self.latest_run(case_id)
        return rows[0] if rows else None


class SqlNegotiationRepository(TenantScopedRepository):
    def create_round(self, **fields: Any) -> NegotiationRoundModel:
        row = NegotiationRoundModel(tenant_id=self.tenant_id, **fields)
        self.session.add(row)
        self.session.flush()
        return row

    def get_round(self, round_id: str) -> NegotiationRoundModel | None:
        row = self.session.get(NegotiationRoundModel, round_id)
        return row if row and row.tenant_id == self.tenant_id else None

    def current_round(self, case_id: str) -> NegotiationRoundModel | None:
        return self.session.scalars(
            self._scoped(select(NegotiationRoundModel), NegotiationRoundModel)
            .where(NegotiationRoundModel.case_id == case_id)
            .order_by(desc(NegotiationRoundModel.round_number))
            .limit(1)
        ).first()

    def list_rounds(self, case_id: str) -> list[NegotiationRoundModel]:
        return list(
            self.session.scalars(
                self._scoped(select(NegotiationRoundModel), NegotiationRoundModel)
                .where(NegotiationRoundModel.case_id == case_id)
                .order_by(NegotiationRoundModel.round_number)
            ).all()
        )

    def add_target(self, round_row: NegotiationRoundModel, **fields: Any) -> NegotiationTargetModel:
        row = NegotiationTargetModel(
            tenant_id=self.tenant_id, round_id=round_row.id, case_id=round_row.case_id, **fields
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get_target(self, round_id: str, vendor_id: str) -> NegotiationTargetModel | None:
        return self.session.scalars(
            select(NegotiationTargetModel).where(
                NegotiationTargetModel.round_id == round_id,
                NegotiationTargetModel.vendor_id == vendor_id,
            )
        ).first()

    def record_response(
        self,
        round_id: str,
        vendor_id: str,
        *,
        response_quotation_id: str,
        achieved_total_base: Decimal,
    ) -> None:
        target = self.get_target(round_id, vendor_id)
        if target is None:
            return
        target.response_quotation_id = response_quotation_id
        target.achieved_total_base = achieved_total_base
        baseline = Decimal(str(target.current_total_base or 0))
        if baseline > 0:
            target.achieved_reduction_pct = (
                (baseline - achieved_total_base) / baseline * Decimal(100)
            ).quantize(Decimal("0.0001"))
        target.status = "RESPONDED"
        target.responded_at = utcnow()
        self.session.flush()

    def close_round(self, round_row: NegotiationRoundModel, *, achieved_total_base: Decimal | None) -> None:
        round_row.status = "CLOSED"
        round_row.closed_at = utcnow()
        if achieved_total_base is not None:
            round_row.achieved_total_base = achieved_total_base
            baseline = Decimal(str(round_row.baseline_total_base or 0))
            if baseline > 0:
                round_row.savings_base = baseline - achieved_total_base
                round_row.savings_pct = (
                    round_row.savings_base / baseline * Decimal(100)
                ).quantize(Decimal("0.0001"))
        self.session.flush()


class SqlPoRecommendationRepository(TenantScopedRepository):
    def create(self, **fields: Any) -> PoRecommendationModel:
        row = PoRecommendationModel(tenant_id=self.tenant_id, **fields)
        self.session.add(row)
        self.session.flush()
        return row

    def add_line(self, recommendation: PoRecommendationModel, **fields: Any) -> PoRecommendationLineModel:
        row = PoRecommendationLineModel(
            tenant_id=self.tenant_id, recommendation_id=recommendation.id, **fields
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get(self, recommendation_id: str) -> PoRecommendationModel | None:
        row = self.session.get(PoRecommendationModel, recommendation_id)
        return row if row and row.tenant_id == self.tenant_id else None

    def latest_for_case(self, case_id: str) -> PoRecommendationModel | None:
        return self.session.scalars(
            self._scoped(select(PoRecommendationModel), PoRecommendationModel)
            .where(PoRecommendationModel.case_id == case_id)
            .order_by(desc(PoRecommendationModel.created_at))
            .limit(1)
        ).first()

    def next_number(self) -> str:
        count = int(
            self.session.scalar(
                self._scoped(
                    select(func.count()).select_from(PoRecommendationModel), PoRecommendationModel
                )
            )
            or 0
        )
        return f"POR-{utcnow().year}-{count + 1:06d}"

    def release(self, recommendation: PoRecommendationModel, *, actor_id: str, erp_po_number: str = "") -> None:
        recommendation.status = "RELEASED"
        recommendation.released_by = actor_id
        recommendation.released_at = utcnow()
        recommendation.erp_po_number = erp_po_number
        self.session.flush()


class SqlInfoRecordProposalRepository(TenantScopedRepository):
    def create(self, **fields: Any) -> InfoRecordProposalModel:
        row = InfoRecordProposalModel(tenant_id=self.tenant_id, **fields)
        self.session.add(row)
        self.session.flush()
        return row

    def get(self, proposal_id: str) -> InfoRecordProposalModel | None:
        row = self.session.get(InfoRecordProposalModel, proposal_id)
        return row if row and row.tenant_id == self.tenant_id else None

    def list_for_case(self, case_id: str) -> list[InfoRecordProposalModel]:
        return list(
            self.session.scalars(
                self._scoped(select(InfoRecordProposalModel), InfoRecordProposalModel).where(
                    InfoRecordProposalModel.case_id == case_id
                )
            ).all()
        )

    def mark_applied(self, proposal_id: str, *, actor_id: str, info_record_id: str) -> None:
        row = self.session.get(InfoRecordProposalModel, proposal_id)
        if row is None:
            return
        row.status = "APPLIED"
        row.applied_by = actor_id
        row.applied_at = utcnow()
        row.resulting_info_record_id = info_record_id
        self.session.flush()


class SqlCommunicationRepository(TenantScopedRepository):
    def create(self, **fields: Any) -> CommunicationModel:
        row = CommunicationModel(tenant_id=self.tenant_id, **fields)
        self.session.add(row)
        self.session.flush()
        return row

    def get(self, communication_id: str) -> CommunicationModel | None:
        row = self.session.get(CommunicationModel, communication_id)
        return row if row and row.tenant_id == self.tenant_id else None

    def find_by_idempotency_key(self, key: str) -> CommunicationModel | None:
        return self.session.scalars(
            self._scoped(select(CommunicationModel), CommunicationModel).where(
                CommunicationModel.idempotency_key == key
            )
        ).first()

    def find_by_external_id(self, message_id: str) -> CommunicationModel | None:
        if not message_id:
            return None
        return self.session.scalars(
            self._scoped(select(CommunicationModel), CommunicationModel).where(
                CommunicationModel.external_message_id == message_id
            )
        ).first()

    def find_by_thread_token(self, token: str) -> list[CommunicationModel]:
        return list(
            self.session.scalars(
                self._scoped(select(CommunicationModel), CommunicationModel)
                .where(CommunicationModel.thread_token == token)
                .order_by(CommunicationModel.created_at)
            ).all()
        )

    def list_for_case(self, case_id: str, *, limit: int = 500) -> list[CommunicationModel]:
        return list(
            self.session.scalars(
                self._scoped(select(CommunicationModel), CommunicationModel)
                .where(CommunicationModel.case_id == case_id)
                .order_by(CommunicationModel.created_at)
                .limit(limit)
            ).all()
        )

    def list_pending_release(self, *, limit: int = 200) -> list[CommunicationModel]:
        return list(
            self.session.scalars(
                self._scoped(select(CommunicationModel), CommunicationModel)
                .where(CommunicationModel.status.in_(["PENDING_APPROVAL", "SUPPRESSED"]))
                .order_by(CommunicationModel.created_at)
                .limit(limit)
            ).all()
        )

    def mark_sent(
        self, communication_id: str, *, provider: str, provider_message_id: str
    ) -> None:
        row = self.get(communication_id)
        if row is None:
            return
        row.status = "SENT"
        row.provider = provider
        row.provider_message_id = provider_message_id
        row.sent_at = utcnow()
        self.session.flush()

    def mark_failed(self, communication_id: str, *, error: str) -> None:
        row = self.get(communication_id)
        if row is None:
            return
        row.status = "FAILED"
        row.error_detail = error[:8000]
        self.session.flush()

    def last_outbound_to_vendor(
        self, case_id: str, vendor_id: str
    ) -> CommunicationModel | None:
        return self.session.scalars(
            self._scoped(select(CommunicationModel), CommunicationModel)
            .where(
                CommunicationModel.case_id == case_id,
                CommunicationModel.vendor_id == vendor_id,
                CommunicationModel.direction == "OUTBOUND",
                CommunicationModel.status.in_(["SENT", "DELIVERED"]),
            )
            .order_by(desc(CommunicationModel.created_at))
            .limit(1)
        ).first()


def _opt_dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))
