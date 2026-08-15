"""Domain entities and the invariants that protect them.

Anything a language model could get catastrophically wrong lives here as
deterministic code: which state transitions are legal, when commercial data may
be unsealed, how many reminders a supplier may receive, and which approvals a
given award value demands. The model proposes; this module decides.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from .enums import (
    ApprovalDecision,
    ApprovalType,
    CaseState,
    ComparisonOperator,
    ComplianceStatus,
    RequirementKind,
    RequirementObligation,
    TrustState,
)
from .errors import (
    DomainInvariantError,
    PolicyViolationError,
    UnsafeTransitionError,
    ValidationError,
)
from .money import Money
from .units import UnitConverter, normalize_uom


def utcnow() -> datetime:
    return datetime.now(UTC)


# Legal state graph. Every edge is a deliberate business rule; anything not
# listed is rejected, including "helpful" shortcuts an agent might attempt.
LEGAL_TRANSITIONS: dict[CaseState, frozenset[CaseState]] = {
    CaseState.RECEIVED: frozenset({CaseState.VALIDATING_PR, CaseState.CANCELLED, CaseState.FAILED}),
    CaseState.VALIDATING_PR: frozenset(
        {
            CaseState.WAITING_FOR_ENGINEERING,
            CaseState.SOURCING_STRATEGY,
            CaseState.CANCELLED,
            CaseState.FAILED,
        }
    ),
    CaseState.WAITING_FOR_ENGINEERING: frozenset(
        {CaseState.VALIDATING_PR, CaseState.SOURCING_STRATEGY, CaseState.CANCELLED, CaseState.FAILED}
    ),
    CaseState.SOURCING_STRATEGY: frozenset(
        {CaseState.READY_FOR_RFQ, CaseState.WAITING_FOR_ENGINEERING, CaseState.CANCELLED, CaseState.FAILED}
    ),
    CaseState.READY_FOR_RFQ: frozenset(
        {CaseState.WAITING_FOR_QUOTES, CaseState.SOURCING_STRATEGY, CaseState.CANCELLED, CaseState.FAILED}
    ),
    CaseState.WAITING_FOR_QUOTES: frozenset(
        {CaseState.TECHNICAL_EVALUATION, CaseState.READY_FOR_RFQ, CaseState.CANCELLED, CaseState.FAILED}
    ),
    CaseState.TECHNICAL_EVALUATION: frozenset(
        {
            CaseState.WAITING_FOR_TECHNICAL_APPROVAL,
            CaseState.WAITING_FOR_QUOTES,
            CaseState.CANCELLED,
            CaseState.FAILED,
        }
    ),
    CaseState.WAITING_FOR_TECHNICAL_APPROVAL: frozenset(
        {
            CaseState.COMMERCIAL_EVALUATION,
            CaseState.TECHNICAL_EVALUATION,
            CaseState.READY_FOR_RFQ,
            CaseState.CANCELLED,
            CaseState.FAILED,
        }
    ),
    CaseState.COMMERCIAL_EVALUATION: frozenset(
        {CaseState.NEGOTIATION, CaseState.WAITING_FOR_AWARD_APPROVAL, CaseState.CANCELLED, CaseState.FAILED}
    ),
    CaseState.NEGOTIATION: frozenset(
        {
            CaseState.NEGOTIATION,
            CaseState.COMMERCIAL_EVALUATION,
            CaseState.WAITING_FOR_AWARD_APPROVAL,
            CaseState.CANCELLED,
            CaseState.FAILED,
        }
    ),
    CaseState.WAITING_FOR_AWARD_APPROVAL: frozenset(
        {
            CaseState.PO_RECOMMENDATION,
            CaseState.NEGOTIATION,
            CaseState.COMMERCIAL_EVALUATION,
            CaseState.CANCELLED,
            CaseState.FAILED,
        }
    ),
    CaseState.PO_RECOMMENDATION: frozenset(
        {CaseState.ORDER_PLACED, CaseState.WAITING_FOR_AWARD_APPROVAL, CaseState.CANCELLED, CaseState.FAILED}
    ),
    CaseState.ORDER_PLACED: frozenset({CaseState.EXPEDITING, CaseState.COMPLETED, CaseState.CANCELLED}),
    CaseState.EXPEDITING: frozenset({CaseState.EXPEDITING, CaseState.COMPLETED, CaseState.CANCELLED}),
    CaseState.COMPLETED: frozenset(),
    CaseState.CANCELLED: frozenset(),
    CaseState.FAILED: frozenset({CaseState.VALIDATING_PR, CaseState.CANCELLED}),
}


@dataclass(slots=True)
class PurchaseRequisitionLine:
    """One requisition line. A PR may request several materials at once."""

    line_number: int
    material_code: str
    quantity: Decimal
    uom: str
    description: str = ""
    required_date: datetime | None = None
    plant_code: str = ""
    storage_location: str = ""
    cost_center: str = ""
    gl_account: str = ""
    estimated_unit_price: Decimal | None = None
    currency: str = ""
    specification_reference: str = ""
    manufacturer_part_number: str = ""
    preferred_vendor_id: str = ""
    free_text_only: bool = False  # no material code; described in prose
    notes: str = ""

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.quantity is None or Decimal(str(self.quantity)) <= 0:
            errors.append(f"Line {self.line_number}: quantity must be positive")
        if not (self.uom or "").strip():
            errors.append(f"Line {self.line_number}: UOM is required")
        if not self.free_text_only and not (self.material_code or "").strip():
            errors.append(f"Line {self.line_number}: material code is required")
        if self.free_text_only and len((self.description or "").strip()) < 10:
            errors.append(
                f"Line {self.line_number}: free-text lines need a description of at least 10 characters"
            )
        if self.required_date and self.required_date < utcnow() - timedelta(days=1):
            errors.append(f"Line {self.line_number}: required date is in the past")
        return errors

    def normalized_uom(self) -> str:
        return normalize_uom(self.uom)


@dataclass(slots=True)
class PurchaseRequisition:
    """A requisition as received from the requester, before ERP validation."""

    pr_number: str
    plant_code: str
    requester: str
    lines: list[PurchaseRequisitionLine] = field(default_factory=list)
    requester_email: str = ""
    department: str = ""
    company_code: str = ""
    currency: str = ""
    priority: str = "NORMAL"
    justification: str = ""
    budget_code: str = ""
    created_at: datetime = field(default_factory=utcnow)
    source_channel: str = "API"  # API | EMAIL | CSV | SAP_EXPORT | UI
    raw_reference: str = ""

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not (self.pr_number or "").strip():
            errors.append("PR number is required")
        elif not re.fullmatch(r"[A-Za-z0-9_\-/]{3,40}", self.pr_number.strip()):
            errors.append(f"PR number {self.pr_number!r} has an unexpected format")
        if not (self.plant_code or "").strip():
            errors.append("Plant code is required")
        if not (self.requester or "").strip():
            errors.append("Requester is required")
        if self.requester_email and not re.fullmatch(
            r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", self.requester_email.strip()
        ):
            errors.append(f"Requester email {self.requester_email!r} is malformed")
        if not self.lines:
            errors.append("At least one requisition line is required")
        seen_lines: set[int] = set()
        for line in self.lines:
            if line.line_number in seen_lines:
                errors.append(f"Duplicate line number {line.line_number}")
            seen_lines.add(line.line_number)
            errors.extend(line.validate())
        if self.priority.upper() not in ("LOW", "NORMAL", "HIGH", "URGENT", "EMERGENCY"):
            errors.append(f"Unknown priority {self.priority!r}")
        return errors

    @property
    def is_valid(self) -> bool:
        return not self.validate()

    def total_estimated_value(self, currency: str) -> Money:
        total = Money.zero(currency)
        for line in self.lines:
            if line.estimated_unit_price is None:
                continue
            line_currency = (line.currency or self.currency or currency).upper()
            if line_currency != currency:
                # Mixed-currency estimates are normalised later, by the
                # commercial layer that owns FX. Skip rather than guess.
                continue
            total = total + Money(
                Decimal(str(line.estimated_unit_price)) * Decimal(str(line.quantity)), currency
            )
        return total

    def line(self, line_number: int) -> PurchaseRequisitionLine:
        for candidate in self.lines:
            if candidate.line_number == line_number:
                return candidate
        raise ValidationError(f"PR {self.pr_number} has no line {line_number}")


@dataclass(slots=True)
class SourcingCase:
    """The agent's durable case file. Guards every consequential transition."""

    case_id: str
    pr_number: str
    state: CaseState = CaseState.RECEIVED
    tenant_id: str = ""
    commercial_unlocked: bool = False
    technical_approved_at: datetime | None = None
    award_approved_at: datetime | None = None
    reminder_count_by_supplier: dict[str, int] = field(default_factory=dict)
    negotiation_round: int = 0
    awarded_supplier_id: str = ""
    estimated_value_base: Decimal = Decimal(0)
    base_currency: str = "USD"
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    version: int = 1
    state_history: list[dict[str, Any]] = field(default_factory=list)
    cancellation_reason: str = ""
    failure_reason: str = ""

    # ------------------------------------------------------------- transitions
    def can_transition(self, new_state: CaseState) -> bool:
        return new_state in LEGAL_TRANSITIONS.get(self.state, frozenset())

    def transition(self, new_state: CaseState, *, actor: str = "SYSTEM", reason: str = "") -> None:
        if new_state == self.state and new_state not in (
            CaseState.NEGOTIATION,
            CaseState.EXPEDITING,
        ):
            return
        if not self.can_transition(new_state):
            raise UnsafeTransitionError(
                f"Illegal case transition {self.state} -> {new_state}",
                case_id=self.case_id,
                from_state=str(self.state),
                to_state=str(new_state),
            )
        # The single most important guard in the system: commercial data can
        # only be worked once a human has signed off the technical evaluation.
        if new_state == CaseState.COMMERCIAL_EVALUATION and not self.commercial_unlocked:
            raise UnsafeTransitionError(
                "Commercial evaluation requires an explicit human technical approval",
                case_id=self.case_id,
            )
        if new_state == CaseState.PO_RECOMMENDATION and self.award_approved_at is None:
            raise UnsafeTransitionError(
                "PO recommendation requires a recorded award approval", case_id=self.case_id
            )
        previous = self.state
        self.state = new_state
        self.updated_at = utcnow()
        self.version += 1
        self.state_history.append(
            {
                "from": str(previous),
                "to": str(new_state),
                "actor": actor,
                "reason": reason,
                "at": self.updated_at.isoformat(),
            }
        )
        if new_state == CaseState.CANCELLED and reason:
            self.cancellation_reason = reason
        if new_state == CaseState.FAILED and reason:
            self.failure_reason = reason

    # ------------------------------------------------------------------- gates
    def unlock_commercial(self, *, actor: str) -> None:
        if self.state != CaseState.WAITING_FOR_TECHNICAL_APPROVAL:
            raise UnsafeTransitionError(
                "Commercial bids can only be unsealed from WAITING_FOR_TECHNICAL_APPROVAL",
                case_id=self.case_id,
                state=str(self.state),
            )
        if not actor or actor == "SYSTEM":
            raise PolicyViolationError(
                "Commercial unlock requires an authenticated human actor", case_id=self.case_id
            )
        self.commercial_unlocked = True
        self.technical_approved_at = utcnow()
        self.updated_at = self.technical_approved_at

    def record_award_approval(self, *, actor: str, supplier_id: str) -> None:
        if self.state != CaseState.WAITING_FOR_AWARD_APPROVAL:
            raise UnsafeTransitionError(
                "Award approval is only valid in WAITING_FOR_AWARD_APPROVAL",
                case_id=self.case_id,
                state=str(self.state),
            )
        if not actor or actor == "SYSTEM":
            raise PolicyViolationError("Award approval requires an authenticated human actor")
        if not supplier_id:
            raise ValidationError("Award approval must name the awarded supplier")
        self.award_approved_at = utcnow()
        self.awarded_supplier_id = supplier_id
        self.updated_at = self.award_approved_at

    def register_reminder(self, supplier_id: str, max_reminders: int) -> int:
        if self.state != CaseState.WAITING_FOR_QUOTES:
            raise DomainInvariantError(
                f"Reminders are only legal while waiting for quotes (state={self.state})",
                case_id=self.case_id,
            )
        sent = self.reminder_count_by_supplier.get(supplier_id, 0)
        if sent >= max_reminders:
            raise DomainInvariantError(
                f"Reminder limit ({max_reminders}) reached for {supplier_id}",
                case_id=self.case_id,
                supplier_id=supplier_id,
            )
        self.reminder_count_by_supplier[supplier_id] = sent + 1
        self.updated_at = utcnow()
        return sent + 1

    def open_negotiation_round(self, max_rounds: int) -> int:
        if self.state not in (CaseState.COMMERCIAL_EVALUATION, CaseState.NEGOTIATION):
            raise UnsafeTransitionError(
                "Negotiation rounds require an unlocked commercial evaluation",
                case_id=self.case_id,
                state=str(self.state),
            )
        if not self.commercial_unlocked:
            raise UnsafeTransitionError("Negotiation requires technical approval first")
        if self.negotiation_round >= max_rounds:
            raise DomainInvariantError(
                f"Negotiation round limit ({max_rounds}) reached", case_id=self.case_id
            )
        self.negotiation_round += 1
        self.updated_at = utcnow()
        return self.negotiation_round

    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal


@dataclass(slots=True)
class Approval:
    """An immutable record of a human decision."""

    approval_id: str
    case_id: str
    approval_type: ApprovalType
    decision: ApprovalDecision
    actor_id: str
    reason: str
    actor_roles: tuple[str, ...] = ()
    subject_ref: str = ""  # supplier id, quotation id, deviation id...
    conditions: tuple[str, ...] = ()
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)
    signature: str = ""  # request-body hash, ties the decision to what was shown

    def validate(self) -> None:
        if not self.actor_id.strip():
            raise ValidationError("Approval requires an actor")
        if self.actor_id == "SYSTEM":
            raise PolicyViolationError("The agent cannot approve on a human's behalf")
        if len(self.reason.strip()) < 3:
            raise ValidationError("Approval requires a reason of at least 3 characters")
        if self.decision == ApprovalDecision.APPROVED_WITH_CONDITIONS and not self.conditions:
            raise ValidationError("Conditional approval must state its conditions")

    @property
    def is_positive(self) -> bool:
        return self.decision in (
            ApprovalDecision.APPROVED,
            ApprovalDecision.APPROVED_WITH_CONDITIONS,
        )


@dataclass(slots=True)
class Requirement:
    """One atomic, checkable requirement extracted from a specification.

    The point of the typed operator/value/tolerance triple is that compliance
    becomes arithmetic rather than an opinion: the model's job is extraction,
    and `evaluate()` below decides pass or fail.
    """

    requirement_id: str
    case_id: str
    kind: RequirementKind
    obligation: RequirementObligation
    attribute: str
    operator: ComparisonOperator
    raw_text: str
    target_value: str = ""
    target_numeric: Decimal | None = None
    upper_numeric: Decimal | None = None
    lower_numeric: Decimal | None = None
    tolerance_plus: Decimal | None = None
    tolerance_minus: Decimal | None = None
    uom: str = ""
    allowed_values: tuple[str, ...] = ()
    weight: Decimal = Decimal(1)
    source_document_version_id: str = ""
    source_location: str = ""
    trust_state: TrustState = TrustState.UNVERIFIED
    extraction_confidence: Decimal = Decimal("0.0")
    created_at: datetime = field(default_factory=utcnow)

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.attribute.strip():
            errors.append(f"{self.requirement_id}: attribute is required")
        numeric_ops = {
            ComparisonOperator.GTE,
            ComparisonOperator.LTE,
            ComparisonOperator.TOLERANCE,
        }
        if self.operator in numeric_ops and self.target_numeric is None:
            errors.append(f"{self.requirement_id}: {self.operator} needs a numeric target")
        if self.operator == ComparisonOperator.RANGE and (
            self.lower_numeric is None or self.upper_numeric is None
        ):
            errors.append(f"{self.requirement_id}: RANGE needs both bounds")
        if self.operator == ComparisonOperator.ONE_OF and not self.allowed_values:
            errors.append(f"{self.requirement_id}: ONE_OF needs allowed values")
        if self.weight <= 0:
            errors.append(f"{self.requirement_id}: weight must be positive")
        return errors

    def acceptable_band(self) -> tuple[Decimal | None, Decimal | None]:
        """Inclusive (low, high) numeric band this requirement accepts."""
        if self.operator == ComparisonOperator.RANGE:
            return self.lower_numeric, self.upper_numeric
        if self.operator == ComparisonOperator.TOLERANCE and self.target_numeric is not None:
            plus = self.tolerance_plus if self.tolerance_plus is not None else Decimal(0)
            minus = self.tolerance_minus if self.tolerance_minus is not None else plus
            return self.target_numeric - minus, self.target_numeric + plus
        if self.operator == ComparisonOperator.GTE:
            return self.target_numeric, None
        if self.operator == ComparisonOperator.LTE:
            return None, self.target_numeric
        if self.operator == ComparisonOperator.EQ and self.target_numeric is not None:
            return self.target_numeric, self.target_numeric
        return None, None

    def evaluate(
        self,
        *,
        offered_text: str | None,
        offered_numeric: Decimal | None = None,
        offered_uom: str = "",
        converter: UnitConverter | None = None,
    ) -> tuple[ComplianceStatus, str]:
        """Deterministically compare a supplier's offer against this requirement.

        Silence is never compliance: an unanswered requirement returns
        NOT_ADDRESSED, which blocks qualification for mandatory items.
        """
        has_text = bool((offered_text or "").strip())
        if offered_numeric is None and not has_text:
            return ComplianceStatus.NOT_ADDRESSED, "Supplier did not address this requirement"

        # Align units before comparing anything numeric. Identical units need no
        # conversion, and must not be routed through the converter: engineering
        # units the converter does not know about would otherwise be reported
        # unverifiable purely because they are unregistered.
        value = offered_numeric
        if (
            value is not None
            and self.uom
            and offered_uom
            and offered_uom.strip().upper() != self.uom.strip().upper()
        ):
            conv = converter or UnitConverter()
            try:
                value = conv.convert(value, offered_uom, self.uom)
            except Exception:
                return (
                    ComplianceStatus.UNVERIFIABLE,
                    f"Offered unit {offered_uom!r} is not convertible to required {self.uom!r}",
                )

        op = self.operator
        if op == ComparisonOperator.PRESENT:
            return ComplianceStatus.COMPLIANT, "Value supplied"

        if op == ComparisonOperator.BOOLEAN:
            truthy = _as_bool(offered_text)
            if truthy is None:
                return ComplianceStatus.UNVERIFIABLE, f"Ambiguous yes/no answer: {offered_text!r}"
            expected = _as_bool(self.target_value)
            expected = True if expected is None else expected
            if truthy == expected:
                return ComplianceStatus.COMPLIANT, "Confirmed"
            return ComplianceStatus.NON_COMPLIANT, f"Expected {expected}, supplier stated {truthy}"

        if op == ComparisonOperator.ONE_OF:
            offered_norm = _norm(offered_text)
            for allowed in self.allowed_values:
                if _norm(allowed) == offered_norm:
                    return ComplianceStatus.COMPLIANT, f"Matches allowed value {allowed!r}"
            return (
                ComplianceStatus.NON_COMPLIANT,
                f"{offered_text!r} is not one of {list(self.allowed_values)}",
            )

        if op == ComparisonOperator.CONTAINS:
            if _norm(self.target_value) in _norm(offered_text):
                return ComplianceStatus.COMPLIANT, f"Contains {self.target_value!r}"
            return ComplianceStatus.NON_COMPLIANT, f"Does not contain {self.target_value!r}"

        if op == ComparisonOperator.EQ and self.target_numeric is None:
            if _norm(offered_text) == _norm(self.target_value):
                return ComplianceStatus.COMPLIANT, "Exact match"
            return (
                ComplianceStatus.DEVIATION,
                f"Offered {offered_text!r} differs from required {self.target_value!r}",
            )

        if value is None:
            return (
                ComplianceStatus.UNVERIFIABLE,
                f"Requirement is numeric but supplier answered {offered_text!r}",
            )

        low, high = self.acceptable_band()
        if low is not None and value < low:
            return (
                ComplianceStatus.NON_COMPLIANT,
                f"Offered {value} {self.uom} is below the minimum {low} {self.uom}",
            )
        if high is not None and value > high:
            # Exceeding a maximum is a real failure; exceeding a minimum is not.
            status = (
                ComplianceStatus.NON_COMPLIANT
                if op in (ComparisonOperator.LTE, ComparisonOperator.RANGE, ComparisonOperator.TOLERANCE)
                else ComplianceStatus.DEVIATION
            )
            return status, f"Offered {value} {self.uom} exceeds the maximum {high} {self.uom}"
        return ComplianceStatus.COMPLIANT, f"Offered {value} {self.uom} satisfies {self.describe()}"

    def describe(self) -> str:
        unit = f" {self.uom}" if self.uom else ""
        match self.operator:
            case ComparisonOperator.GTE:
                return f"{self.attribute} >= {self.target_numeric}{unit}"
            case ComparisonOperator.LTE:
                return f"{self.attribute} <= {self.target_numeric}{unit}"
            case ComparisonOperator.RANGE:
                return f"{self.attribute} in [{self.lower_numeric}, {self.upper_numeric}]{unit}"
            case ComparisonOperator.TOLERANCE:
                return (
                    f"{self.attribute} = {self.target_numeric} "
                    f"+{self.tolerance_plus}/-{self.tolerance_minus}{unit}"
                )
            case ComparisonOperator.ONE_OF:
                return f"{self.attribute} in {list(self.allowed_values)}"
            case ComparisonOperator.BOOLEAN:
                return f"{self.attribute} required"
            case ComparisonOperator.PRESENT:
                return f"{self.attribute} must be stated"
            case _:
                return f"{self.attribute} = {self.target_value}{unit}"

    @property
    def is_mandatory(self) -> bool:
        return self.obligation == RequirementObligation.MANDATORY


@dataclass(slots=True)
class ComplianceAssessment:
    """Result of checking one requirement against one supplier's offer."""

    requirement_id: str
    supplier_id: str
    status: ComplianceStatus
    offered_value: str
    rationale: str
    evidence_ids: tuple[str, ...] = ()
    assessed_by: str = "AGENT"
    deviation_accepted: bool = False
    deviation_approval_id: str = ""
    confidence: Decimal = Decimal("0.0")

    @property
    def blocks_qualification(self) -> bool:
        if self.deviation_accepted:
            return False
        return self.status.is_blocking or self.status == ComplianceStatus.DEVIATION


def _norm(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


_TRUE_TOKENS = {"yes", "y", "true", "compliant", "comply", "complies", "confirmed", "ok", "agreed", "1"}
_FALSE_TOKENS = {"no", "n", "false", "non-compliant", "not compliant", "deviation", "0", "none"}
_FALSE_PHRASES = (
    "not available", "not offered", "not included", "not possible", "cannot", "can not",
    "unable", "not supported", "no bid", "not applicable", "n/a",
)


def _as_bool(value: str | None) -> bool | None:
    """Interpret a yes/no answer.

    Suppliers rarely answer with a bare "no"; they write "No - not available" or
    "Not offered on this model". Reading only exact tokens left those as
    ambiguous, which is materially worse than reading them as the refusal they
    plainly are.
    """
    token = _norm(value)
    if not token:
        return None
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    if any(phrase in token for phrase in _FALSE_PHRASES):
        return False
    first = token.replace("-", " ").split()[0]
    if first in _FALSE_TOKENS:
        return False
    if first in _TRUE_TOKENS:
        return True
    return None
