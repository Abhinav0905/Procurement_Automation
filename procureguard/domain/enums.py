"""Controlled vocabularies shared by the domain, persistence and API layers."""

from __future__ import annotations

from enum import StrEnum


class CaseState(StrEnum):
    """Lifecycle of a sourcing case. Transitions are enforced in entities.py."""

    RECEIVED = "RECEIVED"
    VALIDATING_PR = "VALIDATING_PR"
    WAITING_FOR_ENGINEERING = "WAITING_FOR_ENGINEERING"
    SOURCING_STRATEGY = "SOURCING_STRATEGY"
    READY_FOR_RFQ = "READY_FOR_RFQ"
    WAITING_FOR_QUOTES = "WAITING_FOR_QUOTES"
    TECHNICAL_EVALUATION = "TECHNICAL_EVALUATION"
    WAITING_FOR_TECHNICAL_APPROVAL = "WAITING_FOR_TECHNICAL_APPROVAL"
    COMMERCIAL_EVALUATION = "COMMERCIAL_EVALUATION"
    NEGOTIATION = "NEGOTIATION"
    WAITING_FOR_AWARD_APPROVAL = "WAITING_FOR_AWARD_APPROVAL"
    PO_RECOMMENDATION = "PO_RECOMMENDATION"
    ORDER_PLACED = "ORDER_PLACED"
    EXPEDITING = "EXPEDITING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in (CaseState.COMPLETED, CaseState.CANCELLED, CaseState.FAILED)

    @property
    def is_waiting_on_human(self) -> bool:
        return self in (
            CaseState.WAITING_FOR_ENGINEERING,
            CaseState.WAITING_FOR_TECHNICAL_APPROVAL,
            CaseState.WAITING_FOR_AWARD_APPROVAL,
        )


class ApprovalType(StrEnum):
    PR_EXCEPTION = "PR_EXCEPTION"
    SOURCING_STRATEGY = "SOURCING_STRATEGY"
    RFQ_RELEASE = "RFQ_RELEASE"
    TECHNICAL = "TECHNICAL"
    DEVIATION = "DEVIATION"
    COMMERCIAL_UNLOCK = "COMMERCIAL_UNLOCK"
    NEGOTIATION_SEND = "NEGOTIATION_SEND"
    AWARD = "AWARD"
    PO_RELEASE = "PO_RELEASE"
    SINGLE_SOURCE = "SINGLE_SOURCE"
    PAYMENT_DETAIL_CHANGE = "PAYMENT_DETAIL_CHANGE"


class ApprovalDecision(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    APPROVED_WITH_CONDITIONS = "APPROVED_WITH_CONDITIONS"
    RETURNED_FOR_REWORK = "RETURNED_FOR_REWORK"


class TrustState(StrEnum):
    """Provenance grade of a claim or document version."""

    AUTHORITATIVE = "AUTHORITATIVE"  # ERP / signed engineering master data
    VERIFIED = "VERIFIED"  # human-confirmed
    UNVERIFIED = "UNVERIFIED"  # supplier-asserted, not yet checked
    QUARANTINED = "QUARANTINED"  # failed the document firewall
    SUPERSEDED = "SUPERSEDED"
    CONFLICTED = "CONFLICTED"


class DocumentAuthority(StrEnum):
    ERP_MASTER = "ERP_MASTER"
    ENGINEERING = "ENGINEERING"
    QUALITY = "QUALITY"
    PROCUREMENT = "PROCUREMENT"
    SUPPLIER = "SUPPLIER"
    THIRD_PARTY_CERT = "THIRD_PARTY_CERT"
    UNKNOWN = "UNKNOWN"

    @property
    def is_supplier_controlled(self) -> bool:
        return self in (DocumentAuthority.SUPPLIER, DocumentAuthority.UNKNOWN)


class DocumentType(StrEnum):
    PURCHASE_REQUISITION = "PURCHASE_REQUISITION"
    TECHNICAL_SPECIFICATION = "TECHNICAL_SPECIFICATION"
    ENGINEERING_DRAWING = "ENGINEERING_DRAWING"
    DATASHEET = "DATASHEET"
    BILL_OF_MATERIALS = "BILL_OF_MATERIALS"
    QUALITY_PLAN = "QUALITY_PLAN"
    RFQ = "RFQ"
    QUOTATION = "QUOTATION"
    TECHNICAL_BID = "TECHNICAL_BID"
    COMMERCIAL_BID = "COMMERCIAL_BID"
    CERTIFICATE = "CERTIFICATE"
    TEST_REPORT = "TEST_REPORT"
    NEGOTIATION_LETTER = "NEGOTIATION_LETTER"
    PURCHASE_ORDER = "PURCHASE_ORDER"
    CORRESPONDENCE = "CORRESPONDENCE"
    OTHER = "OTHER"


class RequirementKind(StrEnum):
    DIMENSIONAL = "DIMENSIONAL"
    MATERIAL = "MATERIAL"
    PERFORMANCE = "PERFORMANCE"
    ELECTRICAL = "ELECTRICAL"
    MECHANICAL = "MECHANICAL"
    ENVIRONMENTAL = "ENVIRONMENTAL"
    STANDARD_COMPLIANCE = "STANDARD_COMPLIANCE"
    CERTIFICATION = "CERTIFICATION"
    QUALITY = "QUALITY"
    DOCUMENTATION = "DOCUMENTATION"
    PACKAGING = "PACKAGING"
    DELIVERY = "DELIVERY"
    COMMERCIAL = "COMMERCIAL"
    WARRANTY = "WARRANTY"
    OTHER = "OTHER"


class RequirementObligation(StrEnum):
    MANDATORY = "MANDATORY"  # non-compliance disqualifies
    DESIRABLE = "DESIRABLE"  # scored, does not disqualify
    INFORMATIONAL = "INFORMATIONAL"


class ComparisonOperator(StrEnum):
    EQ = "EQ"
    GTE = "GTE"
    LTE = "LTE"
    RANGE = "RANGE"
    TOLERANCE = "TOLERANCE"
    ONE_OF = "ONE_OF"
    CONTAINS = "CONTAINS"
    BOOLEAN = "BOOLEAN"
    PRESENT = "PRESENT"


class ComplianceStatus(StrEnum):
    COMPLIANT = "COMPLIANT"
    NON_COMPLIANT = "NON_COMPLIANT"
    DEVIATION = "DEVIATION"  # differs but may be acceptable with approval
    NOT_ADDRESSED = "NOT_ADDRESSED"  # supplier silent -> never treated as compliant
    UNVERIFIABLE = "UNVERIFIABLE"  # claimed but evidence quarantined/missing

    @property
    def is_blocking(self) -> bool:
        return self in (
            ComplianceStatus.NON_COMPLIANT,
            ComplianceStatus.NOT_ADDRESSED,
            ComplianceStatus.UNVERIFIABLE,
        )


class RfqStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_RELEASE_APPROVAL = "PENDING_RELEASE_APPROVAL"
    ISSUED = "ISSUED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class RfqInvitationStatus(StrEnum):
    DRAFT = "DRAFT"
    QUEUED = "QUEUED"
    SENT = "SENT"
    DELIVERY_FAILED = "DELIVERY_FAILED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    QUOTED = "QUOTED"
    DECLINED = "DECLINED"
    NO_RESPONSE = "NO_RESPONSE"
    DISQUALIFIED = "DISQUALIFIED"


class QuotationStatus(StrEnum):
    RECEIVED = "RECEIVED"
    PARSING = "PARSING"
    PARSED = "PARSED"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    QUARANTINED = "QUARANTINED"
    SEALED = "SEALED"  # commercial part encrypted until technical approval
    TECHNICALLY_QUALIFIED = "TECHNICALLY_QUALIFIED"
    TECHNICALLY_DISQUALIFIED = "TECHNICALLY_DISQUALIFIED"
    COMMERCIALLY_EVALUATED = "COMMERCIALLY_EVALUATED"
    SUPERSEDED = "SUPERSEDED"
    WITHDRAWN = "WITHDRAWN"
    EXPIRED = "EXPIRED"


class Incoterm(StrEnum):
    EXW = "EXW"
    FCA = "FCA"
    FAS = "FAS"
    FOB = "FOB"
    CFR = "CFR"
    CIF = "CIF"
    CPT = "CPT"
    CIP = "CIP"
    DAP = "DAP"
    DPU = "DPU"
    DDP = "DDP"


class CommunicationType(StrEnum):
    RFQ_INVITATION = "RFQ_INVITATION"
    RFQ_REMINDER = "RFQ_REMINDER"
    CLARIFICATION_REQUEST = "CLARIFICATION_REQUEST"
    CLARIFICATION_RESPONSE = "CLARIFICATION_RESPONSE"
    QUOTATION_RECEIPT = "QUOTATION_RECEIPT"
    NEGOTIATION_ROUND = "NEGOTIATION_ROUND"
    NEGOTIATION_RESPONSE = "NEGOTIATION_RESPONSE"
    REGRET_LETTER = "REGRET_LETTER"
    AWARD_LETTER = "AWARD_LETTER"
    PO_DISPATCH = "PO_DISPATCH"
    DELIVERY_REMINDER = "DELIVERY_REMINDER"
    OVERDUE_ESCALATION = "OVERDUE_ESCALATION"
    INTERNAL_NOTIFICATION = "INTERNAL_NOTIFICATION"
    UNCLASSIFIED = "UNCLASSIFIED"


class CommunicationDirection(StrEnum):
    OUTBOUND = "OUTBOUND"
    INBOUND = "INBOUND"


class CommunicationStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    QUEUED = "QUEUED"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    BOUNCED = "BOUNCED"
    FAILED = "FAILED"
    RECEIVED = "RECEIVED"
    SUPPRESSED = "SUPPRESSED"  # blocked by allow_automated_email_send=false


class DecisionType(StrEnum):
    PR_VALIDATION = "PR_VALIDATION"
    MATERIAL_RESOLUTION = "MATERIAL_RESOLUTION"
    HISTORICAL_BENCHMARK = "HISTORICAL_BENCHMARK"
    REQUIREMENT_EXTRACTION = "REQUIREMENT_EXTRACTION"
    SUPPLIER_SHORTLIST = "SUPPLIER_SHORTLIST"
    RFQ_PACKAGE = "RFQ_PACKAGE"
    TECHNICAL_COMPARISON = "TECHNICAL_COMPARISON"
    COMMERCIAL_NORMALIZATION = "COMMERCIAL_NORMALIZATION"
    BID_RANKING = "BID_RANKING"
    NEGOTIATION_STRATEGY = "NEGOTIATION_STRATEGY"
    PO_RECOMMENDATION = "PO_RECOMMENDATION"
    INFO_RECORD_PROPOSAL = "INFO_RECORD_PROPOSAL"


class EvidenceRole(StrEnum):
    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    CONTEXT = "CONTEXT"
    BASELINE = "BASELINE"
    SOURCE_OF_TRUTH = "SOURCE_OF_TRUTH"


class MaterialStatus(StrEnum):
    ACTIVE = "ACTIVE"
    BLOCKED_FOR_PROCUREMENT = "BLOCKED_FOR_PROCUREMENT"
    OBSOLETE = "OBSOLETE"
    PHASE_OUT = "PHASE_OUT"
    ENGINEERING_HOLD = "ENGINEERING_HOLD"
    NOT_EXTENDED_TO_PLANT = "NOT_EXTENDED_TO_PLANT"

    @property
    def blocks_procurement(self) -> bool:
        return self is not MaterialStatus.ACTIVE


class ProcurementType(StrEnum):
    EXTERNAL = "EXTERNAL"  # F - purchased
    INTERNAL = "INTERNAL"  # E - made in house
    BOTH = "BOTH"  # X


class VendorStatus(StrEnum):
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"
    PENDING_QUALIFICATION = "PENDING_QUALIFICATION"
    DEREGISTERED = "DEREGISTERED"
    ONE_TIME = "ONE_TIME"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class NegotiationRoundStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    SENT = "SENT"
    RESPONDED = "RESPONDED"
    NO_RESPONSE = "NO_RESPONSE"
    CLOSED = "CLOSED"
    ABANDONED = "ABANDONED"


class PoStatus(StrEnum):
    RECOMMENDED = "RECOMMENDED"
    PENDING_RELEASE = "PENDING_RELEASE"
    RELEASED = "RELEASED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_DELIVERED = "PARTIALLY_DELIVERED"
    DELIVERED = "DELIVERED"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class SecuritySeverity(StrEnum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Role(StrEnum):
    """RBAC roles. Mapped from SSO group claims."""

    REQUESTER = "REQUESTER"
    BUYER = "BUYER"
    SENIOR_BUYER = "SENIOR_BUYER"
    CATEGORY_MANAGER = "CATEGORY_MANAGER"
    ENGINEER = "ENGINEER"
    QUALITY = "QUALITY"
    FINANCE = "FINANCE"
    PROCUREMENT_HEAD = "PROCUREMENT_HEAD"
    EXECUTIVE = "EXECUTIVE"
    AUDITOR = "AUDITOR"
    SYSTEM = "SYSTEM"
    ADMIN = "ADMIN"


class Permission(StrEnum):
    CASE_READ = "CASE_READ"
    CASE_CREATE = "CASE_CREATE"
    CASE_CANCEL = "CASE_CANCEL"
    DOCUMENT_UPLOAD = "DOCUMENT_UPLOAD"
    DOCUMENT_READ = "DOCUMENT_READ"
    RFQ_RELEASE = "RFQ_RELEASE"
    EMAIL_SEND = "EMAIL_SEND"
    TECHNICAL_APPROVE = "TECHNICAL_APPROVE"
    DEVIATION_APPROVE = "DEVIATION_APPROVE"
    COMMERCIAL_READ = "COMMERCIAL_READ"
    NEGOTIATION_SEND = "NEGOTIATION_SEND"
    AWARD_APPROVE = "AWARD_APPROVE"
    PO_RELEASE = "PO_RELEASE"
    ADMIN_MANAGE = "ADMIN_MANAGE"
    AUDIT_READ = "AUDIT_READ"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.REQUESTER: frozenset({Permission.CASE_READ, Permission.CASE_CREATE}),
    Role.BUYER: frozenset(
        {
            Permission.CASE_READ,
            Permission.CASE_CREATE,
            Permission.CASE_CANCEL,
            Permission.DOCUMENT_UPLOAD,
            Permission.DOCUMENT_READ,
            Permission.COMMERCIAL_READ,
        }
    ),
    Role.SENIOR_BUYER: frozenset(
        {
            Permission.CASE_READ,
            Permission.CASE_CREATE,
            Permission.CASE_CANCEL,
            Permission.DOCUMENT_UPLOAD,
            Permission.DOCUMENT_READ,
            Permission.COMMERCIAL_READ,
            Permission.RFQ_RELEASE,
            Permission.EMAIL_SEND,
            Permission.NEGOTIATION_SEND,
        }
    ),
    Role.CATEGORY_MANAGER: frozenset(
        {
            Permission.CASE_READ,
            Permission.DOCUMENT_READ,
            Permission.COMMERCIAL_READ,
            Permission.RFQ_RELEASE,
            Permission.EMAIL_SEND,
            Permission.NEGOTIATION_SEND,
            Permission.AWARD_APPROVE,
        }
    ),
    Role.ENGINEER: frozenset(
        {
            Permission.CASE_READ,
            Permission.DOCUMENT_UPLOAD,
            Permission.DOCUMENT_READ,
            Permission.TECHNICAL_APPROVE,
            Permission.DEVIATION_APPROVE,
        }
    ),
    Role.QUALITY: frozenset(
        {
            Permission.CASE_READ,
            Permission.DOCUMENT_READ,
            Permission.TECHNICAL_APPROVE,
            Permission.DEVIATION_APPROVE,
        }
    ),
    Role.FINANCE: frozenset(
        {Permission.CASE_READ, Permission.COMMERCIAL_READ, Permission.AUDIT_READ}
    ),
    Role.PROCUREMENT_HEAD: frozenset(
        {
            Permission.CASE_READ,
            Permission.CASE_CANCEL,
            Permission.DOCUMENT_READ,
            Permission.COMMERCIAL_READ,
            Permission.RFQ_RELEASE,
            Permission.EMAIL_SEND,
            Permission.NEGOTIATION_SEND,
            Permission.AWARD_APPROVE,
            Permission.PO_RELEASE,
            Permission.AUDIT_READ,
        }
    ),
    Role.EXECUTIVE: frozenset(
        {
            Permission.CASE_READ,
            Permission.COMMERCIAL_READ,
            Permission.AWARD_APPROVE,
            Permission.PO_RELEASE,
            Permission.AUDIT_READ,
        }
    ),
    Role.AUDITOR: frozenset(
        {Permission.CASE_READ, Permission.DOCUMENT_READ, Permission.AUDIT_READ}
    ),
    Role.SYSTEM: frozenset({Permission.CASE_READ, Permission.DOCUMENT_READ}),
    Role.ADMIN: frozenset(Permission),
}
