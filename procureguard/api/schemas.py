"""API request and response models."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from procureguard.domain.enums import ApprovalDecision, CaseState


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# ──────────────────────────────────────────────────────────────────── requests

class CreateCaseRequest(ApiModel):
    case_id: str | None = Field(default=None, min_length=3, max_length=64)
    pr_number: str | None = Field(default=None, max_length=40)
    pr_artifact_uri: str = ""
    # Inline requisition payload; alternative to uploading a file.
    requisition: dict[str, Any] | None = None
    plant_code: str = ""
    source_channel: str = "API"
    auto_release_rfq: bool = False
    enable_negotiation: bool = True
    quote_window_days: int = Field(default=10, ge=1, le=120)
    start_workflow: bool = True


class ApprovalRequest(ApiModel):
    actor_id: str | None = None
    reason: str = Field(min_length=3, max_length=4000)
    decision: ApprovalDecision = ApprovalDecision.APPROVED
    subject_ref: str = ""
    conditions: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


class AwardApprovalRequest(ApprovalRequest):
    supplier_id: str = Field(min_length=1, max_length=40)


class DeviationApprovalRequest(ApprovalRequest):
    assessment_id: str = Field(min_length=1)


class SupplierResponseRequest(ApiModel):
    supplier_id: str = Field(min_length=1, max_length=40)


class QuotationTextRequest(ApiModel):
    vendor_id: str = Field(min_length=1, max_length=40)
    text: str = Field(min_length=1)
    negotiation_round: int | None = None
    received_via: str = "MANUAL"


class ShortlistOverrideRequest(ApiModel):
    vendor_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=3, max_length=2000)


class AddSupplierRequest(ApiModel):
    vendor_id: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=3, max_length=2000)


class ReleaseCommunicationRequest(ApiModel):
    reason: str = Field(default="Released by buyer", max_length=2000)


class ReleasePoRequest(ApiModel):
    erp_po_number: str = ""
    reason: str = Field(default="Released for ERP creation", max_length=2000)


class CancelCaseRequest(ApiModel):
    reason: str = Field(min_length=3, max_length=2000)


class RequirementOverrideRequest(ApiModel):
    status: str
    note: str = Field(min_length=3, max_length=2000)


class InboundEmailWebhook(ApiModel):
    """SES/SNS notification, or a raw MIME payload for testing."""

    raw_message: str | None = None
    s3_key: str | None = None
    notification: dict[str, Any] | None = None


class SeedRequest(ApiModel):
    scale: str = "small"
    reset: bool = False


# ─────────────────────────────────────────────────────────────────── responses

class CaseSummary(ApiModel):
    case_id: str
    pr_number: str
    state: CaseState
    title: str = ""
    plant_code: str = ""
    buyer_id: str = ""
    awarded_supplier_id: str = ""
    estimated_value_base: Decimal = Decimal(0)
    awarded_value_base: Decimal = Decimal(0)
    savings_base: Decimal = Decimal(0)
    base_currency: str = "USD"
    negotiation_round: int = 0
    commercial_unlocked: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    due_date: datetime | None = None
    workflow_id: str = ""


class CaseListResponse(ApiModel):
    total: int
    limit: int
    offset: int
    items: list[CaseSummary]


class CaseDetailResponse(ApiModel):
    case: CaseSummary
    requisition: dict[str, Any] | None = None
    requirements: list[dict[str, Any]] = Field(default_factory=list)
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    rfq: dict[str, Any] | None = None
    quotations: list[dict[str, Any]] = Field(default_factory=list)
    ranking: list[dict[str, Any]] = Field(default_factory=list)
    negotiations: list[dict[str, Any]] = Field(default_factory=list)
    po_recommendation: dict[str, Any] | None = None
    approvals: list[dict[str, Any]] = Field(default_factory=list)
    pending_actions: list[dict[str, Any]] = Field(default_factory=list)
    security_findings: list[dict[str, Any]] = Field(default_factory=list)
    workflow: dict[str, Any] | None = None


class ApprovalResponse(ApiModel):
    approval_id: str
    case_id: str
    approval_type: str
    decision: str
    actor_id: str
    status: str
    next_state: str = ""
    detail: str = ""


class WorkflowStateResponse(ApiModel):
    case_id: str
    stage: str
    rfq_released: bool = False
    technical_approved: bool = False
    award_approved: bool = False
    cancelled: bool = False
    quotes_received: int = 0
    l1_vendor_id: str = ""
    supplier_responses: dict[str, bool] = Field(default_factory=dict)
    pending_suppliers: list[str] = Field(default_factory=list)


class HealthResponse(ApiModel):
    status: str
    version: str
    environment: str
    database: dict[str, Any]
    temporal: dict[str, Any]
    backends: dict[str, str]


class ComparisonMatrixResponse(ApiModel):
    case_id: str
    requirements: list[dict[str, Any]]
    evaluations: list[dict[str, Any]]
    cells: dict[str, dict[str, dict[str, Any]]]
    qualified_vendor_ids: list[str]
    warnings: list[str] = Field(default_factory=list)


class BenchmarkResponse(ApiModel):
    material_code: str
    benchmark: dict[str, Any]


def to_case_summary(row: Any) -> CaseSummary:
    return CaseSummary(
        case_id=row.id,
        pr_number=row.pr_number,
        state=CaseState(row.state),
        title=row.title or "",
        plant_code=row.plant_code or "",
        buyer_id=row.buyer_id or "",
        awarded_supplier_id=row.awarded_supplier_id or "",
        estimated_value_base=Decimal(str(row.estimated_value_base or 0)),
        awarded_value_base=Decimal(str(row.awarded_value_base or 0)),
        savings_base=Decimal(str(row.savings_base or 0)),
        base_currency=row.base_currency or "USD",
        negotiation_round=int(row.negotiation_round or 0),
        commercial_unlocked=bool(row.commercial_unlocked),
        created_at=row.created_at,
        updated_at=row.updated_at,
        due_date=row.due_date,
        workflow_id=row.workflow_id or "",
    )
