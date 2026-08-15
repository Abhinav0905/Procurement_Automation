"""Sourcing case routes: create, inspect, upload evidence, drive the pipeline."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from procureguard.api.dependencies import Context, require
from procureguard.api.schemas import (
    AddSupplierRequest,
    CancelCaseRequest,
    CaseDetailResponse,
    CaseListResponse,
    ComparisonMatrixResponse,
    CreateCaseRequest,
    QuotationTextRequest,
    ShortlistOverrideRequest,
    SupplierResponseRequest,
    WorkflowStateResponse,
    to_case_summary,
)
from procureguard.api.temporal_client import (
    query_workflow_state,
    start_procurement_workflow,
    try_signal_workflow,
)
from procureguard.application.document_ingestion import DocumentIngestionService
from procureguard.application.history_service import HistoricalProcurementService
from procureguard.application.pr_intake import RequisitionIntakeService
from procureguard.application.quotation_ingestion import QuotationIngestionService
from procureguard.application.requirements import RequirementExtractionService
from procureguard.application.supplier_shortlist import SupplierShortlistService
from procureguard.application.technical_comparison import TechnicalComparisonService
from procureguard.domain.enums import CaseState, DocumentAuthority, DocumentType, Permission
from procureguard.domain.errors import NotFoundError, ValidationError
from procureguard.observability import logger

log = logger(__name__)

router = APIRouter(prefix="/cases", tags=["cases"])


@router.post("", status_code=201)
async def create_case(
    request: CreateCaseRequest,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.CASE_CREATE))],
) -> dict[str, Any]:
    """Open a case from an inline requisition payload."""
    if not request.requisition:
        raise ValidationError(
            "A requisition payload is required. Use POST /cases/upload for a file."
        )
    payload = dict(request.requisition)
    if request.pr_number and not payload.get("pr_number"):
        payload["pr_number"] = request.pr_number

    result = RequisitionIntakeService(ctx).intake(
        content=json.dumps(payload).encode(),
        filename=f"{payload.get('pr_number', 'requisition')}.json",
        media_type="application/json",
        source_channel=request.source_channel,
        case_id=request.case_id or "",
        default_plant=request.plant_code,
        received_from=principal.email,
    )
    ctx.session.commit()

    workflow_info: dict[str, Any] = {}
    if request.start_workflow:
        try:
            workflow_info = await start_procurement_workflow(
                case_id=result.case_id,
                tenant_id=ctx.tenant_id,
                pr_artifact_uri=request.pr_artifact_uri,
                correlation_id=ctx.correlation_id,
                quote_window_days=request.quote_window_days,
                enable_negotiation=request.enable_negotiation,
                auto_release_rfq=request.auto_release_rfq,
            )
        except Exception as exc:
            # The case exists and is authoritative; orchestration can be started
            # later rather than losing the requisition.
            workflow_info = {"error": str(exc)[:300], "started": False}
            log.warning("workflow_start_failed", case_id=result.case_id, detail=str(exc)[:300])

    return {**result.to_dict(), "workflow": workflow_info}


@router.post("/upload", status_code=201)
async def upload_requisition(
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.CASE_CREATE))],
    file: Annotated[UploadFile, File()],
    plant_code: Annotated[str, Form()] = "",
    source_channel: Annotated[str, Form()] = "UPLOAD",
    start_workflow: Annotated[bool, Form()] = True,
) -> dict[str, Any]:
    """Open a case from an uploaded CSV, JSON or email file."""
    content = await file.read()
    result = RequisitionIntakeService(ctx).intake(
        content=content,
        filename=file.filename or "requisition",
        media_type=file.content_type or "",
        source_channel=source_channel,
        default_plant=plant_code,
        received_from=principal.email,
    )
    ctx.session.commit()

    workflow_info: dict[str, Any] = {}
    if start_workflow:
        try:
            workflow_info = await start_procurement_workflow(
                case_id=result.case_id,
                tenant_id=ctx.tenant_id,
                correlation_id=ctx.correlation_id,
            )
        except Exception as exc:
            workflow_info = {"error": str(exc)[:300], "started": False}
    return {**result.to_dict(), "workflow": workflow_info}


@router.get("", response_model=CaseListResponse)
def list_cases(
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.CASE_READ))],
    state: str | None = None,
    buyer_id: str | None = None,
    plant_code: str | None = None,
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> CaseListResponse:
    rows, total = ctx.repos.cases.search(
        state=state, buyer_id=buyer_id, plant_code=plant_code, query=q, limit=limit, offset=offset
    )
    return CaseListResponse(
        total=total, limit=limit, offset=offset, items=[to_case_summary(r) for r in rows]
    )


@router.get("/summary")
def case_summary(
    ctx: Context, principal: Annotated[Any, Depends(require(Permission.CASE_READ))]
) -> dict[str, Any]:
    """Counts by state, for the dashboard."""
    counts = ctx.repos.cases.counts_by_state()
    waiting = sum(
        count for state, count in counts.items() if CaseState(state).is_waiting_on_human
    )
    return {
        "counts_by_state": counts,
        "total": sum(counts.values()),
        "awaiting_human": waiting,
        "in_flight": sum(
            count for state, count in counts.items() if not CaseState(state).is_terminal
        ),
    }


@router.get("/{case_id}", response_model=CaseDetailResponse)
async def get_case(
    case_id: str,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.CASE_READ))],
) -> CaseDetailResponse:
    row = ctx.repos.cases.get_model(case_id)
    if row is None:
        raise NotFoundError(f"Case {case_id} not found", case_id=case_id)

    case = ctx.repos.cases.require(case_id)
    may_see_commercial = principal.has(Permission.COMMERCIAL_READ) and case.commercial_unlocked

    requisition = ctx.repos.requisitions.get_model(case.pr_number)
    rfq = ctx.repos.rfqs.latest_for_case(case_id)
    quotations = ctx.repos.quotations.list_for_case(
        case_id, commercial_unlocked=may_see_commercial
    )
    recommendation = ctx.repos.po_recommendations.latest_for_case(case_id)

    from procureguard.application.negotiation import NegotiationService

    return CaseDetailResponse(
        case=to_case_summary(row),
        requisition=_requisition_payload(requisition),
        requirements=[_requirement_payload(r) for r in ctx.repos.requirements.list_active(case_id)],
        candidates=[_candidate_payload(c) for c in ctx.repos.candidates.list_for_case(case_id)],
        rfq=_rfq_payload(rfq),
        quotations=[_quotation_payload(q, may_see_commercial) for q in quotations],
        ranking=[_ranking_payload(r) for r in ctx.repos.rankings.latest_run(case_id)],
        negotiations=NegotiationService(ctx).history(case_id),
        po_recommendation=_recommendation_payload(recommendation, may_see_commercial),
        approvals=[
            {
                "approval_id": a.approval_id,
                "approval_type": str(a.approval_type),
                "decision": str(a.decision),
                "actor_id": a.actor_id,
                "reason": a.reason,
                "subject_ref": a.subject_ref,
                "created_at": a.created_at.isoformat(),
            }
            for a in ctx.repos.approvals.list_for_case(case_id)
        ],
        pending_actions=_pending_actions(ctx, case, rfq, recommendation),
        security_findings=[
            {
                "id": f.id,
                "finding_type": f.finding_type,
                "severity": f.severity,
                "detail": f.detail,
                "disposition": f.disposition,
                "acknowledged": bool(f.acknowledged_at),
                "created_at": f.created_at.isoformat(),
            }
            for f in ctx.repos.findings.list_for_case(case_id)
        ],
        workflow=await query_workflow_state(case_id),
    )


@router.get("/{case_id}/workflow", response_model=WorkflowStateResponse)
async def workflow_state(
    case_id: str,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.CASE_READ))],
) -> WorkflowStateResponse:
    state = await query_workflow_state(case_id)
    if state is None:
        row = ctx.repos.cases.get_model(case_id)
        if row is None:
            raise NotFoundError(f"Case {case_id} not found")
        # Temporal unavailable: report the durable state from CockroachDB.
        return WorkflowStateResponse(case_id=case_id, stage=row.state)
    return WorkflowStateResponse(
        case_id=case_id,
        stage=state.get("stage", "UNKNOWN"),
        rfq_released=bool(state.get("rfq_released")),
        technical_approved=bool(state.get("technical_approved")),
        award_approved=bool(state.get("award_approved")),
        cancelled=bool(state.get("cancelled")),
        quotes_received=int(state.get("quotes_received", 0)),
        l1_vendor_id=state.get("l1_vendor_id", "") or "",
        supplier_responses=state.get("supplier_responses", {}) or {},
        pending_suppliers=state.get("pending_suppliers", []) or [],
    )


@router.get("/{case_id}/audit")
def case_audit(
    case_id: str,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.AUDIT_READ))],
    limit: int = Query(default=500, ge=1, le=2000),
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "entries": [
            {
                "action": entry.action,
                "entity_type": entry.entity_type,
                "entity_id": entry.entity_id,
                "actor_id": entry.actor_id,
                "actor_type": entry.actor_type,
                "detail": entry.detail,
                "before": entry.before_state,
                "after": entry.after_state,
                "created_at": entry.created_at.isoformat(),
            }
            for entry in ctx.repos.audit.list_for_case(case_id, limit=limit)
        ],
    }


@router.get("/{case_id}/decisions")
def case_decisions(
    case_id: str,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.CASE_READ))],
) -> dict[str, Any]:
    """Every agent recommendation with its evidence, for explainability."""
    out = []
    for decision in ctx.repos.decisions.list_for_case(case_id):
        out.append(
            {
                "decision_id": decision.id,
                "decision_type": decision.decision_type,
                "sequence": decision.sequence,
                "rationale": decision.rationale,
                "confidence": str(decision.confidence),
                "model_metadata": decision.model_metadata,
                "created_at": decision.created_at.isoformat(),
                "evidence": [
                    {
                        "evidence_type": e.evidence_type,
                        "evidence_id": e.evidence_id,
                        "role": e.role,
                        "excerpt": e.excerpt,
                    }
                    for e in ctx.repos.decisions.evidence_for(decision.id)
                ],
            }
        )
    return {"case_id": case_id, "decisions": out}


@router.post("/{case_id}/documents", status_code=201)
async def upload_document(
    case_id: str,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.DOCUMENT_UPLOAD))],
    file: Annotated[UploadFile, File()],
    document_type: Annotated[str, Form()] = DocumentType.TECHNICAL_SPECIFICATION.value,
    authority: Annotated[str, Form()] = DocumentAuthority.ENGINEERING.value,
) -> dict[str, Any]:
    """Stage 4: upload an engineering document onto the case."""
    content = await file.read()
    result = DocumentIngestionService(ctx).ingest(
        content=content,
        filename=file.filename or "document",
        case_id=case_id,
        document_type=document_type,
        authority=authority,
        media_type=file.content_type or "",
    )
    return result.to_dict()


@router.get("/{case_id}/documents")
def list_documents(
    case_id: str,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.DOCUMENT_READ))],
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "documents": [
            {
                "document_id": document.id,
                "version_id": version.id,
                "logical_name": document.logical_name,
                "document_type": document.document_type,
                "authority": version.authority,
                "trust_state": version.trust_state,
                "firewall_verdict": version.firewall_verdict,
                "findings": version.firewall_findings,
                "byte_size": version.byte_size,
                "page_count": version.page_count,
                "extraction_method": version.extraction_method,
                "created_at": version.created_at.isoformat(),
            }
            for document, version in ctx.repos.documents.list_for_case(case_id)
        ],
    }


@router.post("/{case_id}/documents/search")
def search_evidence(
    case_id: str,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.DOCUMENT_READ))],
    q: str = Query(min_length=2),
    top_k: int = Query(default=8, ge=1, le=50),
) -> dict[str, Any]:
    """Hybrid vector + keyword retrieval over the case's evidence."""
    hits = DocumentIngestionService(ctx).retrieve(query=q, case_id=case_id, top_k=top_k)
    return {"case_id": case_id, "query": q, "hits": hits}


@router.post("/{case_id}/requirements/extract")
def extract_requirements(
    case_id: str,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.DOCUMENT_UPLOAD))],
) -> dict[str, Any]:
    """Stage 5, on demand (the workflow also runs this automatically)."""
    result = RequirementExtractionService(ctx).extract_for_case(case_id)
    return result.to_dict()


@router.post("/{case_id}/shortlist/rebuild")
def rebuild_shortlist(
    case_id: str,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.CASE_READ))],
) -> dict[str, Any]:
    """Stage 6, on demand."""
    case = ctx.repos.cases.require(case_id)
    pr = ctx.repos.requisitions.get(case.pr_number)
    if pr is None or not pr.lines:
        raise ValidationError("Requisition has no lines", case_id=case_id)

    line = pr.lines[0]
    benchmark = HistoricalProcurementService(ctx).build_benchmark(
        line.material_code,
        requested_quantity=Decimal(str(line.quantity)),
        requested_uom=line.uom,
        plant_code=line.plant_code or pr.plant_code,
    )
    result = SupplierShortlistService(ctx).build(
        case_id=case_id,
        material_code=line.material_code,
        plant_code=line.plant_code or pr.plant_code,
        benchmark=benchmark,
        requirement_text=line.description,
        preferred_vendor_id=line.preferred_vendor_id,
    )
    return result.to_dict()


@router.post("/{case_id}/shortlist/select")
def override_shortlist(
    case_id: str,
    request: ShortlistOverrideRequest,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.RFQ_RELEASE))],
) -> dict[str, Any]:
    """Human override of the agent's supplier selection."""
    changed = ctx.repos.candidates.set_selection(
        case_id, request.vendor_ids, actor_id=principal.actor_id
    )
    ctx.audit(
        entity_type="SUPPLIER_SHORTLIST",
        entity_id=case_id,
        case_id=case_id,
        action="SHORTLIST_OVERRIDDEN",
        after_state={"vendor_ids": request.vendor_ids},
        detail=request.reason,
    )
    return {"case_id": case_id, "selected": request.vendor_ids, "changed": changed}


@router.post("/{case_id}/shortlist/add")
def add_supplier(
    case_id: str,
    request: AddSupplierRequest,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.RFQ_RELEASE))],
) -> dict[str, Any]:
    vendor = ctx.repos.vendors.get(request.vendor_id)
    if vendor is None:
        raise NotFoundError(f"Vendor {request.vendor_id} not found")
    ctx.repos.candidates.add_manual(
        case_id,
        vendor_id=vendor.vendor_id,
        vendor_name=vendor.name,
        actor_id=principal.actor_id,
        rationale=request.reason,
    )
    ctx.audit(
        entity_type="SUPPLIER_SHORTLIST",
        entity_id=case_id,
        case_id=case_id,
        action="SUPPLIER_ADDED_MANUALLY",
        after_state={"vendor_id": vendor.vendor_id},
        detail=request.reason,
    )
    return {"case_id": case_id, "vendor_id": vendor.vendor_id, "added": True}


@router.get("/{case_id}/comparison", response_model=ComparisonMatrixResponse)
def technical_comparison(
    case_id: str,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.CASE_READ))],
    recompute: bool = False,
) -> ComparisonMatrixResponse:
    """Stage 10 output: the requirement x supplier matrix."""
    if recompute:
        matrix = TechnicalComparisonService(ctx).evaluate_case(case_id)
        return ComparisonMatrixResponse(**matrix.to_dict())

    requirements = ctx.repos.requirements.list_active(case_id)
    assessments = ctx.repos.compliance.list_for_case(case_id)
    quotations = {q.id: q for q in ctx.repos.quotations.list_for_case(case_id)}

    cells: dict[str, dict[str, dict[str, Any]]] = {}
    for assessment in assessments:
        cells.setdefault(assessment.requirement_id, {})[assessment.vendor_id] = {
            "assessment_id": assessment.id,
            "status": assessment.status,
            "offered_value": assessment.offered_value,
            "rationale": assessment.rationale,
            "confidence": str(assessment.confidence),
            "deviation_accepted": assessment.deviation_accepted,
            "reviewer_override_status": assessment.reviewer_override_status,
        }
    evaluations = [
        {
            "vendor_id": q.vendor_id,
            "vendor_name": q.vendor_name,
            "quotation_id": q.id,
            "qualified": bool(q.technically_qualified),
            "technical_score": str(q.technical_score) if q.technical_score is not None else None,
            "blockers": list(q.disqualification_reasons or []),
        }
        for q in quotations.values()
    ]
    return ComparisonMatrixResponse(
        case_id=case_id,
        requirements=[_requirement_payload(r) for r in requirements],
        evaluations=evaluations,
        cells=cells,
        qualified_vendor_ids=[e["vendor_id"] for e in evaluations if e["qualified"]],
        warnings=[],
    )


@router.post("/{case_id}/quotations", status_code=201)
def ingest_quotation(
    case_id: str,
    request: QuotationTextRequest,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.DOCUMENT_UPLOAD))],
) -> dict[str, Any]:
    """Stage 9 manual entry: paste a quotation a supplier sent by other means."""
    result = QuotationIngestionService(ctx).ingest_text(
        case_id=case_id,
        vendor_id=request.vendor_id,
        text=request.text,
        negotiation_round=request.negotiation_round,
        received_via=request.received_via,
    )
    return result.to_dict()


@router.post("/{case_id}/supplier-response")
async def supplier_response(
    case_id: str,
    request: SupplierResponseRequest,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.CASE_READ))],
) -> dict[str, Any]:
    signal = await try_signal_workflow(case_id, "supplier_response_received", request.supplier_id)
    return {"accepted": True, "workflow": signal}


@router.post("/{case_id}/engineering-ready")
async def engineering_ready(
    case_id: str,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.DOCUMENT_UPLOAD))],
    note: str = "",
) -> dict[str, Any]:
    """Engineering signals that the specification gap is closed."""
    ctx.audit(
        entity_type="SOURCING_CASE",
        entity_id=case_id,
        case_id=case_id,
        action="ENGINEERING_INPUT_PROVIDED",
        detail=note,
    )
    signal = await try_signal_workflow(case_id, "engineering_information_received", note)
    return {"case_id": case_id, "accepted": True, "workflow": signal}


@router.post("/{case_id}/cancel")
async def cancel_case(
    case_id: str,
    request: CancelCaseRequest,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.CASE_CANCEL))],
) -> dict[str, Any]:
    case = ctx.repos.cases.require(case_id)
    case.transition(CaseState.CANCELLED, actor=principal.actor_id, reason=request.reason)
    ctx.repos.cases.save(case)
    ctx.repos.reminders.cancel_for_case(case_id)
    ctx.audit(
        entity_type="SOURCING_CASE",
        entity_id=case_id,
        case_id=case_id,
        action="CASE_CANCELLED",
        after_state={"state": str(case.state)},
        detail=request.reason,
    )
    signal = await try_signal_workflow(case_id, "cancel", request.reason)
    return {"case_id": case_id, "state": str(case.state), "workflow": signal}


# ────────────────────────────────────────────────────────────────── payloads

def _requisition_payload(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "pr_number": row.pr_number,
        "plant_code": row.plant_code,
        "requester": row.requester,
        "requester_email": row.requester_email,
        "department": row.department,
        "priority": row.priority,
        "currency": row.currency,
        "justification": row.justification,
        "source_channel": row.source_channel,
        "parse_confidence": str(row.parse_confidence),
        "parse_warnings": row.parse_warnings,
        "validation_errors": row.validation_errors,
        "lines": [
            {
                "line_number": line.line_number,
                "material_code": line.material_code,
                "resolved_material_code": line.resolved_material_code,
                "description": line.description,
                "quantity": str(line.quantity),
                "uom": line.uom,
                "normalized_uom": line.normalized_uom,
                "required_date": line.required_date.isoformat() if line.required_date else None,
                "plant_code": line.plant_code,
                "validation_status": line.validation_status,
                "validation_messages": line.validation_messages,
                "resolution_method": line.resolution_method,
                "resolution_confidence": str(line.resolution_confidence),
            }
            for line in sorted(row.lines, key=lambda x: x.line_number)
        ],
    }


def _requirement_payload(row: Any) -> dict[str, Any]:
    return {
        "requirement_id": row.id,
        "requirement_key": row.requirement_key,
        "kind": row.kind,
        "obligation": row.obligation,
        "attribute": row.attribute,
        "operator": row.operator,
        "target_value": row.target_value,
        "target_numeric": str(row.target_numeric) if row.target_numeric is not None else None,
        "lower_numeric": str(row.lower_numeric) if row.lower_numeric is not None else None,
        "upper_numeric": str(row.upper_numeric) if row.upper_numeric is not None else None,
        "tolerance_plus": str(row.tolerance_plus) if row.tolerance_plus is not None else None,
        "tolerance_minus": str(row.tolerance_minus) if row.tolerance_minus is not None else None,
        "uom": row.uom,
        "allowed_values": row.allowed_values,
        "weight": str(row.weight),
        "raw_text": row.raw_text,
        "source_location": row.source_location,
        "trust_state": row.trust_state,
        "extraction_confidence": str(row.extraction_confidence),
    }


def _candidate_payload(row: Any) -> dict[str, Any]:
    return {
        "vendor_id": row.vendor_id,
        "vendor_name": row.vendor_name,
        "rank": row.rank,
        "selected": row.selected,
        "total_score": str(row.total_score),
        "score_breakdown": row.score_breakdown,
        "rationale": row.rationale,
        "selection_source": row.selection_source,
        "excluded_reason": row.excluded_reason,
        "purchase_count_36m": row.purchase_count_36m,
        "last_unit_price_base": (
            str(row.last_unit_price_base) if row.last_unit_price_base is not None else None
        ),
    }


def _rfq_payload(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "rfq_id": row.id,
        "rfq_number": row.rfq_number,
        "status": row.status,
        "revision": row.revision,
        "response_deadline": row.response_deadline.isoformat(),
        "required_incoterm": row.required_incoterm,
        "payment_terms_target": row.payment_terms_target,
        "sealed_bid": row.sealed_bid,
        "released_by": row.released_by,
        "released_at": row.released_at.isoformat() if row.released_at else None,
        "lines": [
            {
                "line_number": line.line_number,
                "material_code": line.material_code,
                "description": line.description,
                "quantity": str(line.quantity),
                "uom": line.uom,
            }
            for line in sorted(row.lines, key=lambda x: x.line_number)
        ],
        "invitations": [
            {
                "vendor_id": inv.vendor_id,
                "vendor_name": inv.vendor_name,
                "contact_email": inv.contact_email,
                "status": inv.status,
                "sent_at": inv.sent_at.isoformat() if inv.sent_at else None,
                "reminders_sent": inv.reminders_sent,
                "responded_at": inv.responded_at.isoformat() if inv.responded_at else None,
            }
            for inv in row.invitations
        ],
    }


def _quotation_payload(row: Any, may_see_commercial: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "quotation_id": row.id,
        "vendor_id": row.vendor_id,
        "vendor_name": row.vendor_name,
        "status": row.status,
        "revision": row.revision,
        "negotiation_round": row.negotiation_round,
        "received_at": row.received_at.isoformat() if row.received_at else None,
        "is_sealed": row.is_sealed,
        "technically_qualified": row.technically_qualified,
        "technical_score": str(row.technical_score) if row.technical_score is not None else None,
        "disqualification_reasons": row.disqualification_reasons,
        "parse_confidence": str(row.parse_confidence),
        "parse_warnings": row.parse_warnings,
        "incoterm": row.incoterm,
        "lead_time_days": row.lead_time_days,
        "validity_days": row.validity_days,
    }
    if may_see_commercial and not row.is_sealed:
        payload.update(
            {
                "currency": row.currency,
                "total_amount": str(row.total_amount),
                "payment_terms": row.payment_terms,
                "freight_amount": str(row.freight_amount),
                "lines": [
                    {
                        "rfq_line_number": line.rfq_line_number,
                        "material_code": line.material_code,
                        "offered_description": line.offered_description,
                        "quantity": str(line.quantity),
                        "uom": line.uom,
                        "unit_price": str(line.unit_price),
                        "price_per_quantity": str(line.price_per_quantity),
                        "currency": line.currency,
                        "line_total": str(line.line_total),
                    }
                    for line in row.lines
                ],
            }
        )
    else:
        payload["commercial_visibility"] = (
            "SEALED - commercial data is unavailable until the technical evaluation is approved"
        )
    return payload


def _ranking_payload(row: Any) -> dict[str, Any]:
    return {
        "position": row.position,
        "position_label": row.position_label,
        "vendor_id": row.vendor_id,
        "vendor_name": row.vendor_name,
        "total_base": str(row.total_base),
        "landed_cost_base": str(row.landed_cost_base),
        "tco_base": str(row.tco_base),
        "delta_vs_l1_base": str(row.delta_vs_l1_base),
        "delta_vs_l1_pct": str(row.delta_vs_l1_pct),
        "delta_vs_benchmark_pct": (
            str(row.delta_vs_benchmark_pct) if row.delta_vs_benchmark_pct is not None else None
        ),
        "technical_score": str(row.technical_score) if row.technical_score is not None else None,
        "weighted_value_score": (
            str(row.weighted_value_score) if row.weighted_value_score is not None else None
        ),
        "technically_qualified": row.technically_qualified,
        "partial_offer": row.partial_offer,
        "flags": row.flags,
    }


def _recommendation_payload(row: Any, may_see_commercial: bool) -> dict[str, Any] | None:
    if row is None:
        return None
    payload: dict[str, Any] = {
        "recommendation_id": row.id,
        "recommendation_number": row.recommendation_number,
        "status": row.status,
        "vendor_id": row.vendor_id,
        "vendor_name": row.vendor_name,
        "approval_chain": row.approval_chain,
        "approval_chain_satisfied": row.approval_chain_satisfied,
        "justification": row.justification,
        "released_by": row.released_by,
        "released_at": row.released_at.isoformat() if row.released_at else None,
        "erp_po_number": row.erp_po_number,
    }
    if may_see_commercial:
        payload.update(
            {
                "currency": row.currency,
                "total_amount": str(row.total_amount),
                "total_amount_base": str(row.total_amount_base),
                "savings_vs_benchmark_base": str(row.savings_vs_benchmark_base),
                "savings_vs_first_offer_base": str(row.savings_vs_first_offer_base),
                "incoterm": row.incoterm,
                "payment_terms": row.payment_terms,
                "sap_payload": row.sap_payload,
                "lines": [
                    {
                        "line_number": line.line_number,
                        "material_code": line.material_code,
                        "description": line.description,
                        "quantity": str(line.quantity),
                        "uom": line.uom,
                        "unit_price": str(line.unit_price),
                        "currency": line.currency,
                        "line_total": str(line.line_total),
                        "price_variance_pct": (
                            str(line.price_variance_pct)
                            if line.price_variance_pct is not None
                            else None
                        ),
                    }
                    for line in sorted(row.lines, key=lambda x: x.line_number)
                ],
            }
        )
    return payload


def _pending_actions(ctx: Any, case: Any, rfq: Any, recommendation: Any) -> list[dict[str, Any]]:
    """What a human is currently being asked to do."""
    actions: list[dict[str, Any]] = []
    if case.state == CaseState.WAITING_FOR_ENGINEERING:
        actions.append(
            {
                "action": "ENGINEERING_INPUT",
                "endpoint": f"/api/v1/cases/{case.case_id}/engineering-ready",
                "permission": "DOCUMENT_UPLOAD",
                "description": "Provide the missing specification or resolve the material issue",
            }
        )
    if rfq is not None and rfq.status == "PENDING_RELEASE_APPROVAL":
        actions.append(
            {
                "action": "RFQ_RELEASE",
                "endpoint": f"/api/v1/cases/{case.case_id}/approvals/rfq-release",
                "permission": "RFQ_RELEASE",
                "description": f"Review and release {rfq.rfq_number} to {len(rfq.invitations)} suppliers",
            }
        )
    if case.state == CaseState.WAITING_FOR_TECHNICAL_APPROVAL:
        actions.append(
            {
                "action": "TECHNICAL_APPROVAL",
                "endpoint": f"/api/v1/cases/{case.case_id}/approvals/technical",
                "permission": "TECHNICAL_APPROVE",
                "description": "Approve the technical evaluation; this unseals the commercial bids",
            }
        )
    if case.state == CaseState.WAITING_FOR_AWARD_APPROVAL:
        actions.append(
            {
                "action": "AWARD_APPROVAL",
                "endpoint": f"/api/v1/cases/{case.case_id}/approvals/award",
                "permission": "AWARD_APPROVE",
                "description": "Approve the recommended award",
            }
        )
    if recommendation is not None and recommendation.status == "RECOMMENDED":
        actions.append(
            {
                "action": "PO_RELEASE",
                "endpoint": f"/api/v1/cases/{case.case_id}/po/release",
                "permission": "PO_RELEASE",
                "description": f"Release {recommendation.recommendation_number} into the ERP",
                "blocked": not recommendation.approval_chain_satisfied,
            }
        )
    pending_mail = [
        c for c in ctx.repos.communications.list_for_case(case.case_id)
        if c.status in ("PENDING_APPROVAL", "SUPPRESSED")
    ]
    if pending_mail:
        actions.append(
            {
                "action": "RELEASE_EMAIL",
                "endpoint": "/api/v1/mail/pending",
                "permission": "EMAIL_SEND",
                "description": f"{len(pending_mail)} outbound message(s) awaiting human release",
                "count": len(pending_mail),
            }
        )
    return actions
