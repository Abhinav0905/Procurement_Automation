"""Deterministic procurement policy.

This is the layer that says "no" to the agent. Every rule is pure, total and
unit-testable: no I/O, no model calls, no clocks beyond what is passed in. If a
behaviour matters to audit or to money, it belongs here rather than in a prompt.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .entities import Approval, ComplianceAssessment, Requirement, SourcingCase
from .enums import (
    ROLE_PERMISSIONS,
    ApprovalType,
    CaseState,
    Permission,
    Role,
)
from .errors import DomainInvariantError, PolicyViolationError


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    reason: str
    required_approvals: tuple[ApprovalType, ...] = ()
    required_permissions: tuple[Permission, ...] = ()

    @classmethod
    def allow(cls, reason: str = "Permitted by policy") -> PolicyDecision:
        return cls(True, reason)

    @classmethod
    def deny(cls, reason: str, **_: object) -> PolicyDecision:
        return cls(False, reason)

    def raise_if_denied(self) -> None:
        if not self.allowed:
            raise PolicyViolationError(self.reason)


@dataclass(frozen=True, slots=True)
class ApprovalRequirement:
    approval_type: ApprovalType
    eligible_roles: tuple[Role, ...]
    minimum_approvers: int
    reason: str


@dataclass(frozen=True, slots=True)
class ProcurementPolicy:
    """Company procurement rules, sourced from Settings at construction time."""

    max_rfq_reminders: int = 2
    reminder_interval_hours: int = 72
    min_suppliers_per_rfq: int = 3
    max_suppliers_per_rfq: int = 6
    min_quotes_to_evaluate: int = 2
    max_negotiation_rounds: int = 3
    dual_approval_threshold: Decimal = Decimal(50_000)
    executive_approval_threshold: Decimal = Decimal(250_000)
    single_source_justification_required: bool = True
    allow_automated_email_send: bool = False
    allow_automated_po_creation: bool = False
    price_increase_alert_pct: Decimal = Decimal(10)
    quote_window_days: int = 10

    @classmethod
    def from_settings(cls, settings) -> ProcurementPolicy:  # noqa: ANN001 - avoids config import cycle
        return cls(
            max_rfq_reminders=settings.max_rfq_reminders,
            reminder_interval_hours=settings.reminder_interval_hours,
            min_suppliers_per_rfq=settings.min_suppliers_per_rfq,
            max_suppliers_per_rfq=settings.max_suppliers_per_rfq,
            min_quotes_to_evaluate=settings.min_quotes_to_evaluate,
            max_negotiation_rounds=settings.max_negotiation_rounds,
            dual_approval_threshold=Decimal(str(settings.dual_approval_threshold)),
            executive_approval_threshold=Decimal(str(settings.executive_approval_threshold)),
            single_source_justification_required=settings.single_source_justification_required,
            allow_automated_email_send=settings.allow_automated_email_send,
            allow_automated_po_creation=settings.allow_automated_po_creation,
            price_increase_alert_pct=Decimal(str(settings.price_increase_alert_pct)),
            quote_window_days=settings.quote_window_days,
        )

    # -------------------------------------------------------------- RFQ / email
    def may_issue_rfq(self, case: SourcingCase, supplier_ids: Sequence[str]) -> PolicyDecision:
        if case.state != CaseState.READY_FOR_RFQ:
            return PolicyDecision.deny(f"RFQ issue is illegal in state {case.state}")
        unique = {s for s in supplier_ids if s}
        if not unique:
            return PolicyDecision.deny("An RFQ needs at least one supplier")
        if len(unique) > self.max_suppliers_per_rfq:
            return PolicyDecision.deny(
                f"{len(unique)} suppliers exceeds the maximum of {self.max_suppliers_per_rfq}"
            )
        if len(unique) < self.min_suppliers_per_rfq and self.single_source_justification_required:
            return PolicyDecision(
                allowed=True,
                reason=(
                    f"Only {len(unique)} supplier(s) invited, below the competitive minimum of "
                    f"{self.min_suppliers_per_rfq}; a documented single/limited-source "
                    f"justification is required before release"
                ),
                required_approvals=(ApprovalType.SINGLE_SOURCE, ApprovalType.RFQ_RELEASE),
            )
        return PolicyDecision(
            allowed=True,
            reason="Competitive supplier set",
            required_approvals=(ApprovalType.RFQ_RELEASE,),
            required_permissions=(Permission.RFQ_RELEASE,),
        )

    def may_send_reminder(
        self, case: SourcingCase, supplier_id: str, *, last_contact_at: datetime | None = None, now: datetime | None = None
    ) -> PolicyDecision:
        if case.state != CaseState.WAITING_FOR_QUOTES:
            return PolicyDecision.deny(f"Reminders are illegal in state {case.state}")
        sent = case.reminder_count_by_supplier.get(supplier_id, 0)
        if sent >= self.max_rfq_reminders:
            return PolicyDecision.deny(
                f"Supplier {supplier_id} already received {sent} reminders (limit {self.max_rfq_reminders})"
            )
        if last_contact_at is not None:
            now = now or datetime.now(UTC)
            earliest = last_contact_at + timedelta(hours=self.reminder_interval_hours)
            if now < earliest:
                return PolicyDecision.deny(
                    f"Reminder interval not elapsed; next reminder allowed at {earliest.isoformat()}"
                )
        return PolicyDecision.allow(f"Reminder {sent + 1} of {self.max_rfq_reminders}")

    def may_transmit_email(self, *, is_external: bool) -> PolicyDecision:
        """External transmission is off unless explicitly enabled by an operator."""
        if not is_external:
            return PolicyDecision.allow("Internal notification")
        if not self.allow_automated_email_send:
            return PolicyDecision.deny(
                "Automated external email is disabled (ALLOW_AUTOMATED_EMAIL_SEND=false); "
                "the message is stored for human release"
            )
        return PolicyDecision.allow("External send enabled by configuration")

    # --------------------------------------------------------------- evaluation
    def may_open_technical_evaluation(
        self, case: SourcingCase, *, quotes_received: int, deadline_passed: bool
    ) -> PolicyDecision:
        if case.state != CaseState.WAITING_FOR_QUOTES:
            return PolicyDecision.deny(f"Technical evaluation is illegal in state {case.state}")
        if quotes_received == 0:
            return PolicyDecision.deny("No quotations received")
        if quotes_received < self.min_quotes_to_evaluate and not deadline_passed:
            return PolicyDecision.deny(
                f"Only {quotes_received} quote(s); waiting for {self.min_quotes_to_evaluate} "
                f"or for the response deadline"
            )
        if quotes_received < self.min_quotes_to_evaluate:
            return PolicyDecision(
                allowed=True,
                reason=(
                    f"Proceeding with {quotes_received} quote(s) after deadline; competition was "
                    f"insufficient and requires justification at award"
                ),
                required_approvals=(ApprovalType.SINGLE_SOURCE,),
            )
        return PolicyDecision.allow(f"{quotes_received} quotations available")

    @staticmethod
    def qualifies_technically(
        assessments: Sequence[ComplianceAssessment], requirements: Sequence[Requirement]
    ) -> tuple[bool, tuple[str, ...]]:
        """A supplier qualifies only if every mandatory requirement is satisfied.

        Missing answers count as failures. Deviations count as failures until a
        human with DEVIATION_APPROVE explicitly accepts them.
        """
        mandatory = {r.requirement_id for r in requirements if r.is_mandatory}
        by_requirement = {a.requirement_id: a for a in assessments}
        blockers: list[str] = []
        for requirement_id in sorted(mandatory):
            assessment = by_requirement.get(requirement_id)
            if assessment is None:
                blockers.append(f"{requirement_id}: not assessed")
                continue
            if assessment.blocks_qualification:
                blockers.append(f"{requirement_id}: {assessment.status} - {assessment.rationale}")
        return (not blockers), tuple(blockers)

    def may_unlock_commercial(self, case: SourcingCase, approval: Approval) -> PolicyDecision:
        if approval.approval_type != ApprovalType.TECHNICAL:
            return PolicyDecision.deny("Commercial unlock requires a TECHNICAL approval")
        if not approval.is_positive:
            return PolicyDecision.deny("Technical approval was not granted")
        if case.state != CaseState.WAITING_FOR_TECHNICAL_APPROVAL:
            return PolicyDecision.deny(f"Case is in {case.state}, not awaiting technical approval")
        return PolicyDecision.allow("Technical approval recorded")

    @classmethod
    def apply_technical_approval(cls, case: SourcingCase, approval: Approval) -> None:
        """Unseal commercial bids and advance the case. The only legal path in."""
        approval.validate()
        policy = cls()
        policy.may_unlock_commercial(case, approval).raise_if_denied()
        case.unlock_commercial(actor=approval.actor_id)
        case.transition(
            CaseState.COMMERCIAL_EVALUATION,
            actor=approval.actor_id,
            reason=f"Technical approval {approval.approval_id}",
        )

    # -------------------------------------------------------------- negotiation
    def may_open_negotiation(self, case: SourcingCase) -> PolicyDecision:
        if not case.commercial_unlocked:
            return PolicyDecision.deny("Negotiation requires technical approval first")
        if case.state not in (CaseState.COMMERCIAL_EVALUATION, CaseState.NEGOTIATION):
            return PolicyDecision.deny(f"Negotiation is illegal in state {case.state}")
        if case.negotiation_round >= self.max_negotiation_rounds:
            return PolicyDecision.deny(
                f"Negotiation round limit ({self.max_negotiation_rounds}) reached"
            )
        return PolicyDecision(
            allowed=True,
            reason=f"Round {case.negotiation_round + 1} of {self.max_negotiation_rounds}",
            required_approvals=(ApprovalType.NEGOTIATION_SEND,),
            required_permissions=(Permission.NEGOTIATION_SEND,),
        )

    # -------------------------------------------------------------------- award
    def approval_chain_for_award(
        self, *, award_value_base: Decimal, is_single_source: bool, has_deviations: bool
    ) -> tuple[ApprovalRequirement, ...]:
        """Who must sign, given the money and the risk on the table."""
        chain: list[ApprovalRequirement] = [
            ApprovalRequirement(
                approval_type=ApprovalType.AWARD,
                eligible_roles=(Role.CATEGORY_MANAGER, Role.PROCUREMENT_HEAD, Role.EXECUTIVE),
                minimum_approvers=1,
                reason="Standard award authorisation",
            )
        ]
        if award_value_base >= self.dual_approval_threshold:
            chain.append(
                ApprovalRequirement(
                    approval_type=ApprovalType.AWARD,
                    eligible_roles=(Role.PROCUREMENT_HEAD, Role.FINANCE),
                    minimum_approvers=1,
                    reason=(
                        f"Award value {award_value_base} meets the dual-approval threshold "
                        f"{self.dual_approval_threshold}"
                    ),
                )
            )
        if award_value_base >= self.executive_approval_threshold:
            chain.append(
                ApprovalRequirement(
                    approval_type=ApprovalType.AWARD,
                    eligible_roles=(Role.EXECUTIVE,),
                    minimum_approvers=1,
                    reason=(
                        f"Award value {award_value_base} meets the executive threshold "
                        f"{self.executive_approval_threshold}"
                    ),
                )
            )
        if is_single_source and self.single_source_justification_required:
            chain.append(
                ApprovalRequirement(
                    approval_type=ApprovalType.SINGLE_SOURCE,
                    eligible_roles=(Role.PROCUREMENT_HEAD, Role.CATEGORY_MANAGER),
                    minimum_approvers=1,
                    reason="Competition was absent or insufficient",
                )
            )
        if has_deviations:
            chain.append(
                ApprovalRequirement(
                    approval_type=ApprovalType.DEVIATION,
                    eligible_roles=(Role.ENGINEER, Role.QUALITY),
                    minimum_approvers=1,
                    reason="Award carries accepted technical deviations",
                )
            )
        return tuple(chain)

    def award_chain_satisfied(
        self, chain: Sequence[ApprovalRequirement], approvals: Sequence[Approval]
    ) -> tuple[bool, tuple[str, ...]]:
        positive = [a for a in approvals if a.is_positive]
        missing: list[str] = []
        consumed: set[str] = set()
        for requirement in chain:
            matches = [
                a
                for a in positive
                if a.approval_type == requirement.approval_type
                and a.approval_id not in consumed
                and (
                    not requirement.eligible_roles
                    or any(role in requirement.eligible_roles for role in _roles(a))
                )
            ]
            if len(matches) < requirement.minimum_approvers:
                missing.append(
                    f"{requirement.approval_type}: needs {requirement.minimum_approvers} from "
                    f"{[r.value for r in requirement.eligible_roles]} ({requirement.reason})"
                )
                continue
            for approval in matches[: requirement.minimum_approvers]:
                consumed.add(approval.approval_id)
        return (not missing), tuple(missing)

    def may_recommend_po(
        self, case: SourcingCase, *, chain_satisfied: bool
    ) -> PolicyDecision:
        if case.state != CaseState.WAITING_FOR_AWARD_APPROVAL:
            return PolicyDecision.deny(f"PO recommendation is illegal in state {case.state}")
        if not chain_satisfied:
            return PolicyDecision.deny("The award approval chain is not fully satisfied")
        if case.award_approved_at is None:
            return PolicyDecision.deny("No award approval is recorded on the case")
        return PolicyDecision.allow("Award approved; PO may be drafted")

    def may_create_po_in_erp(self) -> PolicyDecision:
        if not self.allow_automated_po_creation:
            return PolicyDecision.deny(
                "Automated PO creation is disabled (ALLOW_AUTOMATED_PO_CREATION=false); "
                "ProcureGuard emits a draft for human release into SAP"
            )
        return PolicyDecision(
            allowed=True,
            reason="Automated PO creation enabled",
            required_approvals=(ApprovalType.PO_RELEASE,),
            required_permissions=(Permission.PO_RELEASE,),
        )

    # -------------------------------------------------------------- price sanity
    def price_variance_flags(
        self, *, quoted_unit_price: Decimal, benchmark_unit_price: Decimal | None
    ) -> tuple[str, ...]:
        if benchmark_unit_price is None or benchmark_unit_price <= 0:
            return ("NO_HISTORICAL_BENCHMARK",)
        delta_pct = (quoted_unit_price - benchmark_unit_price) / benchmark_unit_price * Decimal(100)
        flags: list[str] = []
        if delta_pct >= self.price_increase_alert_pct:
            flags.append(f"PRICE_INCREASE_{delta_pct.quantize(Decimal('0.1'))}PCT")
        if delta_pct <= Decimal(-40):
            # Suspiciously cheap usually means a scope misunderstanding.
            flags.append(f"ABNORMALLY_LOW_{delta_pct.quantize(Decimal('0.1'))}PCT")
        return tuple(flags)


def _roles(approval: Approval) -> tuple[Role, ...]:
    out: list[Role] = []
    for raw in approval.actor_roles:
        try:
            out.append(Role(raw))
        except ValueError:
            continue
    return tuple(out)


def permissions_for_roles(roles: Sequence[str]) -> frozenset[Permission]:
    granted: set[Permission] = set()
    for raw in roles:
        try:
            role = Role(raw)
        except ValueError:
            continue
        granted |= ROLE_PERMISSIONS.get(role, frozenset())
    return frozenset(granted)


def require_permission(roles: Sequence[str], permission: Permission) -> None:
    if permission not in permissions_for_roles(roles):
        raise PolicyViolationError(
            f"Permission {permission} is not granted to roles {list(roles)}",
            required=str(permission),
            roles=list(roles),
        )


def assert_approval_is_human(approval: Approval) -> None:
    if approval.actor_id.upper() in ("SYSTEM", "AGENT", "PROCUREGUARD", "BOT"):
        raise DomainInvariantError(
            "Consequential approvals must be recorded against a human identity",
            actor_id=approval.actor_id,
        )


__all__ = [
    "ApprovalRequirement",
    "PolicyDecision",
    "ProcurementPolicy",
    "assert_approval_is_human",
    "permissions_for_roles",
    "require_permission",
]
