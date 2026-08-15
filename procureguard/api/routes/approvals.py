"""Human approval routes - the gates the agent cannot open.

Ordering matters in every handler: the approval is written to CockroachDB first
and is authoritative, then the workflow is signalled. If Temporal is down, the
approval still stands and the workflow resyncs from durable state rather than
the human's decision being silently lost.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from procureguard.api.dependencies import Context, require
from procureguard.api.schemas import (
    ApprovalRequest,
    ApprovalResponse,
    AwardApprovalRequest,
    DeviationApprovalRequest,
    ReleasePoRequest,
)
from procureguard.api.temporal_client import try_signal_workflow
from procureguard.application.quotation_ingestion import QuotationIngestionService
from procureguard.domain.entities import Approval
from procureguard.domain.enums import ApprovalDecision, ApprovalType, CaseState, Permission
from procureguard.domain.errors import ValidationError
from procureguard.domain.policies import ProcurementPolicy
from procureguard.observability import logger

log = logger(__name__)

router = APIRouter(prefix="/cases/{case_id}/approvals", tags=["approvals"])


def _record(
    ctx: Any,
    principal: Any,
    request_obj: Request,
    *,
    case_id: str,
    approval_type: ApprovalType,
    body: ApprovalRequest,
    subject_ref: str = "",
) -> Approval:
    """Create and persist an approval, signed against what was shown."""
    principal.require_human(f"{approval_type} approval")
    approval = Approval(
        approval_id=str(uuid.uuid4()),
        case_id=case_id,
        approval_type=approval_type,
        decision=body.decision,
        actor_id=principal.actor_id,
        reason=body.reason,
        actor_roles=tuple(principal.roles),
        subject_ref=subject_ref or body.subject_ref,
        conditions=tuple(body.conditions),
        payload=dict(body.payload),
        # Ties the decision to the exact payload the approver submitted, so a
        # later "I never agreed to that" is checkable.
        signature=hashlib.sha256(
            json.dumps(body.model_dump(mode="json"), sort_keys=True, default=str).encode()
        ).hexdigest(),
    )
    approval.validate()
    ctx.repos.approvals.add(
        approval,
        ip_address=(request_obj.client.host if request_obj.client else ""),
        user_agent=request_obj.headers.get("user-agent", ""),
    )
    ctx.audit(
        entity_type="APPROVAL",
        entity_id=approval.approval_id,
        case_id=case_id,
        action=f"{approval_type}_{body.decision}",
        after_state={
            "approval_type": str(approval_type),
            "decision": str(body.decision),
            "subject_ref": approval.subject_ref,
        },
        detail=body.reason,
        ip_address=(request_obj.client.host if request_obj.client else ""),
    )
    return approval


@router.post("/rfq-release", response_model=ApprovalResponse)
async def approve_rfq_release(
    case_id: str,
    body: ApprovalRequest,
    request: Request,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.RFQ_RELEASE))],
) -> ApprovalResponse:
    """Release the RFQ to suppliers. The first outward-facing commitment."""
    rfq = ctx.repos.rfqs.latest_for_case(case_id)
    if rfq is None:
        raise ValidationError("No RFQ has been prepared for this case", case_id=case_id)

    approval = _record(
        ctx, principal, request, case_id=case_id, approval_type=ApprovalType.RFQ_RELEASE,
        body=body, subject_ref=rfq.rfq_number,
    )
    if not approval.is_positive:
        return ApprovalResponse(
            approval_id=approval.approval_id, case_id=case_id,
            approval_type=str(ApprovalType.RFQ_RELEASE), decision=str(body.decision),
            actor_id=principal.actor_id, status="REJECTED",
            detail="RFQ was not released; revise the package and resubmit",
        )

    ctx.repos.rfqs.release(rfq, actor_id=principal.actor_id)
    ctx.session.commit()
    signal = await try_signal_workflow(case_id, "rfq_released", approval.approval_id)
    return ApprovalResponse(
        approval_id=approval.approval_id, case_id=case_id,
        approval_type=str(ApprovalType.RFQ_RELEASE), decision=str(body.decision),
        actor_id=principal.actor_id, status="RELEASED",
        detail=f"{rfq.rfq_number} released; workflow signalled={signal['signalled']}",
    )


@router.post("/technical", response_model=ApprovalResponse)
async def approve_technical(
    case_id: str,
    body: ApprovalRequest,
    request: Request,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.TECHNICAL_APPROVE))],
) -> ApprovalResponse:
    """The pivotal gate: approving the technical evaluation unseals the prices."""
    case = ctx.repos.cases.require(case_id)
    approval = _record(
        ctx, principal, request, case_id=case_id, approval_type=ApprovalType.TECHNICAL, body=body
    )

    if not approval.is_positive:
        ctx.session.commit()
        return ApprovalResponse(
            approval_id=approval.approval_id, case_id=case_id,
            approval_type=str(ApprovalType.TECHNICAL), decision=str(body.decision),
            actor_id=principal.actor_id, status="REJECTED",
            detail="Technical evaluation rejected; bids remain sealed",
        )

    # Domain policy performs the unlock and the state transition, or refuses.
    ProcurementPolicy.apply_technical_approval(case, approval)
    ctx.repos.cases.save(case)
    unsealed = QuotationIngestionService(ctx).unseal_case(case_id, actor_id=principal.actor_id)
    ctx.session.commit()

    signal = await try_signal_workflow(case_id, "technical_approval_received", principal.actor_id)
    log.info(
        "technical_approved",
        case_id=case_id,
        actor_id=principal.actor_id,
        unsealed=unsealed,
    )
    return ApprovalResponse(
        approval_id=approval.approval_id, case_id=case_id,
        approval_type=str(ApprovalType.TECHNICAL), decision=str(body.decision),
        actor_id=principal.actor_id, status="APPROVED",
        next_state=str(case.state),
        detail=(
            f"{unsealed} commercial envelope(s) unsealed; workflow "
            f"signalled={signal['signalled']}"
        ),
    )


@router.post("/deviation", response_model=ApprovalResponse)
async def approve_deviation(
    case_id: str,
    body: DeviationApprovalRequest,
    request: Request,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.DEVIATION_APPROVE))],
) -> ApprovalResponse:
    """Accept a specific technical deviation, one requirement at a time."""
    approval = _record(
        ctx, principal, request, case_id=case_id, approval_type=ApprovalType.DEVIATION,
        body=body, subject_ref=body.assessment_id,
    )
    if not approval.is_positive:
        ctx.session.commit()
        return ApprovalResponse(
            approval_id=approval.approval_id, case_id=case_id,
            approval_type=str(ApprovalType.DEVIATION), decision=str(body.decision),
            actor_id=principal.actor_id, status="REJECTED",
            detail="Deviation not accepted; the supplier remains non-compliant on this point",
        )

    assessment = ctx.repos.compliance.accept_deviation(
        body.assessment_id,
        approval_id=approval.approval_id,
        reviewer_id=principal.actor_id,
        note=body.reason,
    )
    if assessment is None:
        raise ValidationError(f"Assessment {body.assessment_id} not found", case_id=case_id)
    ctx.session.commit()
    return ApprovalResponse(
        approval_id=approval.approval_id, case_id=case_id,
        approval_type=str(ApprovalType.DEVIATION), decision=str(body.decision),
        actor_id=principal.actor_id, status="ACCEPTED",
        detail=(
            f"Deviation accepted for {assessment.vendor_id}; re-run the technical comparison "
            f"to refresh qualification"
        ),
    )


@router.post("/negotiation", response_model=ApprovalResponse)
async def approve_negotiation(
    case_id: str,
    body: ApprovalRequest,
    request: Request,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.NEGOTIATION_SEND))],
) -> ApprovalResponse:
    """Authorise a price ask to go out under a named human's authority."""
    round_row = ctx.repos.negotiations.current_round(case_id)
    if round_row is None:
        raise ValidationError("No negotiation round is drafted for this case", case_id=case_id)

    approval = _record(
        ctx, principal, request, case_id=case_id, approval_type=ApprovalType.NEGOTIATION_SEND,
        body=body, subject_ref=str(round_row.round_number),
    )
    ctx.session.commit()
    if not approval.is_positive:
        return ApprovalResponse(
            approval_id=approval.approval_id, case_id=case_id,
            approval_type=str(ApprovalType.NEGOTIATION_SEND), decision=str(body.decision),
            actor_id=principal.actor_id, status="REJECTED",
            detail="Negotiation round was not authorised",
        )

    signal = await try_signal_workflow(case_id, "negotiation_approved", approval.approval_id)
    return ApprovalResponse(
        approval_id=approval.approval_id, case_id=case_id,
        approval_type=str(ApprovalType.NEGOTIATION_SEND), decision=str(body.decision),
        actor_id=principal.actor_id, status="APPROVED",
        detail=(
            f"Round {round_row.round_number} authorised; workflow signalled={signal['signalled']}"
        ),
    )


@router.post("/award", response_model=ApprovalResponse)
async def approve_award(
    case_id: str,
    body: AwardApprovalRequest,
    request: Request,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.AWARD_APPROVE))],
) -> ApprovalResponse:
    """Approve the award. May require several approvers by value."""
    case = ctx.repos.cases.require(case_id)
    approval = _record(
        ctx, principal, request, case_id=case_id, approval_type=ApprovalType.AWARD,
        body=body, subject_ref=body.supplier_id,
    )
    if not approval.is_positive:
        ctx.session.commit()
        return ApprovalResponse(
            approval_id=approval.approval_id, case_id=case_id,
            approval_type=str(ApprovalType.AWARD), decision=str(body.decision),
            actor_id=principal.actor_id, status="REJECTED", detail="Award not approved",
        )

    ranking = ctx.repos.rankings.latest_run(case_id)
    winner = next((r for r in ranking if r.vendor_id == body.supplier_id), None)
    award_value = winner.total_base if winner else case.estimated_value_base
    chain = ctx.policy.approval_chain_for_award(
        award_value_base=award_value,
        is_single_source=len([r for r in ranking if r.technically_qualified])
        < ctx.policy.min_suppliers_per_rfq,
        has_deviations=any(
            a.deviation_accepted for a in ctx.repos.compliance.list_for_case(case_id)
        ),
    )
    approvals = ctx.repos.approvals.list_for_case(case_id)
    satisfied, missing = ctx.policy.award_chain_satisfied(chain, approvals)

    if not satisfied:
        ctx.session.commit()
        return ApprovalResponse(
            approval_id=approval.approval_id, case_id=case_id,
            approval_type=str(ApprovalType.AWARD), decision=str(body.decision),
            actor_id=principal.actor_id, status="PENDING_FURTHER_APPROVAL",
            detail="Recorded, but the approval chain is not yet complete: " + "; ".join(missing),
        )

    case.record_award_approval(actor=principal.actor_id, supplier_id=body.supplier_id)
    ctx.repos.cases.save(case, awarded_supplier_id=body.supplier_id)
    ctx.session.commit()

    signal = await try_signal_workflow(case_id, "award_approval_received", body.supplier_id)
    log.info(
        "award_approved",
        case_id=case_id,
        supplier_id=body.supplier_id,
        actor_id=principal.actor_id,
        value=str(award_value),
    )
    return ApprovalResponse(
        approval_id=approval.approval_id, case_id=case_id,
        approval_type=str(ApprovalType.AWARD), decision=str(body.decision),
        actor_id=principal.actor_id, status="APPROVED",
        next_state=str(case.state),
        detail=(
            f"Award to {body.supplier_id} approved; workflow signalled={signal['signalled']}"
        ),
    )


@router.get("/chain")
def award_chain(
    case_id: str,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.CASE_READ))],
) -> dict[str, Any]:
    """Who still has to sign, and why."""
    case = ctx.repos.cases.require(case_id)
    ranking = ctx.repos.rankings.latest_run(case_id)
    qualified = [r for r in ranking if r.technically_qualified]
    award_value = qualified[0].total_base if qualified else case.estimated_value_base
    chain = ctx.policy.approval_chain_for_award(
        award_value_base=award_value,
        is_single_source=len(qualified) < ctx.policy.min_suppliers_per_rfq,
        has_deviations=any(
            a.deviation_accepted for a in ctx.repos.compliance.list_for_case(case_id)
        ),
    )
    approvals = ctx.repos.approvals.list_for_case(case_id)
    satisfied, missing = ctx.policy.award_chain_satisfied(chain, approvals)
    return {
        "case_id": case_id,
        "award_value_base": str(award_value),
        "satisfied": satisfied,
        "missing": list(missing),
        "chain": [
            {
                "approval_type": str(item.approval_type),
                "eligible_roles": [str(r) for r in item.eligible_roles],
                "minimum_approvers": item.minimum_approvers,
                "reason": item.reason,
            }
            for item in chain
        ],
        "recorded": [
            {
                "approval_type": str(a.approval_type),
                "decision": str(a.decision),
                "actor_id": a.actor_id,
                "roles": list(a.actor_roles),
                "created_at": a.created_at.isoformat(),
            }
            for a in approvals
        ],
    }


po_router = APIRouter(prefix="/cases/{case_id}/po", tags=["purchase-order"])


@po_router.post("/release")
def release_po(
    case_id: str,
    body: ReleasePoRequest,
    request: Request,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.PO_RELEASE))],
) -> dict[str, Any]:
    """Release the draft PO into the ERP. The last human gate."""
    from procureguard.application.po_recommendation import PoRecommendationService

    recommendation = ctx.repos.po_recommendations.latest_for_case(case_id)
    if recommendation is None:
        raise ValidationError("No PO recommendation exists for this case", case_id=case_id)

    approval = _record(
        ctx, principal, request, case_id=case_id, approval_type=ApprovalType.PO_RELEASE,
        body=ApprovalRequest(reason=body.reason, decision=ApprovalDecision.APPROVED),
        subject_ref=recommendation.recommendation_number,
    )
    ctx.session.flush()

    result = PoRecommendationService(ctx).release(
        recommendation_id=recommendation.id,
        actor_id=principal.actor_id,
        erp_po_number=body.erp_po_number,
    )
    case = ctx.repos.cases.require(case_id)
    if case.state == CaseState.PO_RECOMMENDATION:
        case.transition(
            CaseState.ORDER_PLACED, actor=principal.actor_id, reason="Purchase order released"
        )
        ctx.repos.cases.save(case)
    ctx.session.commit()
    return {**result, "approval_id": approval.approval_id, "case_state": str(case.state)}


@po_router.post("/info-records/{proposal_id}/apply")
def apply_info_record(
    case_id: str,
    proposal_id: str,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.PO_RELEASE))],
) -> dict[str, Any]:
    """Write the negotiated price back into purchasing master data."""
    from procureguard.application.po_recommendation import PoRecommendationService

    result = PoRecommendationService(ctx).apply_info_record_proposal(
        proposal_id=proposal_id, actor_id=principal.actor_id
    )
    ctx.session.commit()
    return result


@po_router.get("/info-records")
def list_info_record_proposals(
    case_id: str,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.CASE_READ))],
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "proposals": [
            {
                "proposal_id": p.id,
                "material_code": p.material_code,
                "vendor_id": p.vendor_id,
                "action": p.action,
                "net_price": str(p.net_price),
                "currency": p.currency,
                "order_uom": p.order_uom,
                "previous_net_price": (
                    str(p.previous_net_price) if p.previous_net_price is not None else None
                ),
                "price_change_pct": (
                    str(p.price_change_pct) if p.price_change_pct is not None else None
                ),
                "status": p.status,
                "valid_from": p.valid_from.isoformat(),
                "valid_to": p.valid_to.isoformat() if p.valid_to else None,
            }
            for p in ctx.repos.info_record_proposals.list_for_case(case_id)
        ],
    }
