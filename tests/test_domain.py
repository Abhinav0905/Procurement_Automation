"""Domain invariants - the rules that stop the agent doing damage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from procureguard.domain.entities import (
    Approval,
    ComplianceAssessment,
    PurchaseRequisition,
    PurchaseRequisitionLine,
    Requirement,
    SourcingCase,
)
from procureguard.domain.enums import (
    ApprovalDecision,
    ApprovalType,
    CaseState,
    ComparisonOperator,
    ComplianceStatus,
    RequirementKind,
    RequirementObligation,
)
from procureguard.domain.errors import (
    DomainInvariantError,
    PolicyViolationError,
    UnsafeTransitionError,
    ValidationError,
)
from procureguard.domain.policies import ProcurementPolicy


def _case(state: CaseState = CaseState.RECEIVED) -> SourcingCase:
    return SourcingCase(case_id="PG-1", pr_number="PR-1", state=state)


def _approval(
    approval_type: ApprovalType = ApprovalType.TECHNICAL,
    actor: str = "priya.engineer",
    decision: ApprovalDecision = ApprovalDecision.APPROVED,
    roles: tuple[str, ...] = ("ENGINEER",),
) -> Approval:
    return Approval(
        approval_id="A1", case_id="PG-1", approval_type=approval_type, decision=decision,
        actor_id=actor, reason="Reviewed the evaluation and the deviations", actor_roles=roles,
    )


# ── the central guarantee ────────────────────────────────────────────────────

def test_commercial_evaluation_requires_technical_approval():
    case = _case(CaseState.WAITING_FOR_TECHNICAL_APPROVAL)
    with pytest.raises(UnsafeTransitionError):
        case.transition(CaseState.COMMERCIAL_EVALUATION)


def test_technical_approval_unlocks_commercial():
    case = _case(CaseState.WAITING_FOR_TECHNICAL_APPROVAL)
    ProcurementPolicy.apply_technical_approval(case, _approval())
    assert case.commercial_unlocked
    assert case.state == CaseState.COMMERCIAL_EVALUATION
    assert case.technical_approved_at is not None


def test_agent_cannot_approve_on_a_humans_behalf():
    case = _case(CaseState.WAITING_FOR_TECHNICAL_APPROVAL)
    with pytest.raises(PolicyViolationError):
        case.unlock_commercial(actor="SYSTEM")


def test_rejected_technical_approval_does_not_unlock():
    case = _case(CaseState.WAITING_FOR_TECHNICAL_APPROVAL)
    rejected = _approval(decision=ApprovalDecision.REJECTED)
    with pytest.raises(PolicyViolationError):
        ProcurementPolicy.apply_technical_approval(case, rejected)
    assert not case.commercial_unlocked


def test_po_recommendation_requires_recorded_award_approval():
    case = _case(CaseState.WAITING_FOR_AWARD_APPROVAL)
    with pytest.raises(UnsafeTransitionError):
        case.transition(CaseState.PO_RECOMMENDATION)


def test_award_approval_records_supplier_and_permits_po():
    case = _case(CaseState.WAITING_FOR_AWARD_APPROVAL)
    case.record_award_approval(actor="jordan.head", supplier_id="V1")
    case.transition(CaseState.PO_RECOMMENDATION)
    assert case.awarded_supplier_id == "V1"


def test_illegal_state_jump_is_refused():
    case = _case(CaseState.RECEIVED)
    with pytest.raises(UnsafeTransitionError):
        case.transition(CaseState.PO_RECOMMENDATION)


# ── reminders ────────────────────────────────────────────────────────────────

def test_reminder_limit_is_enforced():
    case = _case(CaseState.WAITING_FOR_QUOTES)
    assert case.register_reminder("V1", 2) == 1
    assert case.register_reminder("V1", 2) == 2
    with pytest.raises(DomainInvariantError):
        case.register_reminder("V1", 2)


def test_reminders_illegal_outside_quote_window():
    case = _case(CaseState.TECHNICAL_EVALUATION)
    with pytest.raises(DomainInvariantError):
        case.register_reminder("V1", 2)


def test_reminder_interval_is_respected():
    policy = ProcurementPolicy(reminder_interval_hours=72)
    case = _case(CaseState.WAITING_FOR_QUOTES)
    now = datetime.now(UTC)
    too_soon = policy.may_send_reminder(case, "V1", last_contact_at=now - timedelta(hours=1), now=now)
    assert not too_soon.allowed
    elapsed = policy.may_send_reminder(case, "V1", last_contact_at=now - timedelta(hours=80), now=now)
    assert elapsed.allowed


# ── negotiation ──────────────────────────────────────────────────────────────

def test_negotiation_requires_unlocked_commercial():
    case = _case(CaseState.COMMERCIAL_EVALUATION)
    with pytest.raises(UnsafeTransitionError):
        case.open_negotiation_round(3)


def test_negotiation_round_limit():
    case = _case(CaseState.WAITING_FOR_TECHNICAL_APPROVAL)
    ProcurementPolicy.apply_technical_approval(case, _approval())
    for expected in (1, 2):
        assert case.open_negotiation_round(2) == expected
    with pytest.raises(DomainInvariantError):
        case.open_negotiation_round(2)


# ── approval chain by value ──────────────────────────────────────────────────

def test_high_value_award_requires_more_approvers():
    policy = ProcurementPolicy(
        dual_approval_threshold=Decimal(50_000), executive_approval_threshold=Decimal(250_000)
    )
    small = policy.approval_chain_for_award(
        award_value_base=Decimal(10_000), is_single_source=False, has_deviations=False
    )
    large = policy.approval_chain_for_award(
        award_value_base=Decimal(500_000), is_single_source=False, has_deviations=False
    )
    assert len(small) == 1
    assert len(large) == 3


def test_award_chain_not_satisfied_by_one_signature_when_two_required():
    policy = ProcurementPolicy(dual_approval_threshold=Decimal(1_000))
    chain = policy.approval_chain_for_award(
        award_value_base=Decimal(5_000), is_single_source=False, has_deviations=False
    )
    one = [_approval(ApprovalType.AWARD, "alex.category", roles=("CATEGORY_MANAGER",))]
    satisfied, missing = policy.award_chain_satisfied(chain, one)
    assert not satisfied and missing

    two = one + [
        Approval(
            approval_id="A2", case_id="PG-1", approval_type=ApprovalType.AWARD,
            decision=ApprovalDecision.APPROVED, actor_id="jordan.head",
            reason="Second approval for value", actor_roles=("PROCUREMENT_HEAD",),
        )
    ]
    satisfied, missing = policy.award_chain_satisfied(chain, two)
    assert satisfied and not missing


def test_one_approval_cannot_satisfy_two_chain_steps():
    """A single signature must not be counted twice."""
    policy = ProcurementPolicy(dual_approval_threshold=Decimal(1_000))
    chain = policy.approval_chain_for_award(
        award_value_base=Decimal(5_000), is_single_source=False, has_deviations=False
    )
    duplicate = [_approval(ApprovalType.AWARD, "jordan.head", roles=("PROCUREMENT_HEAD",))]
    satisfied, _ = policy.award_chain_satisfied(chain, duplicate)
    assert not satisfied


# ── requirement evaluation ───────────────────────────────────────────────────

def _requirement(**kwargs) -> Requirement:
    defaults = dict(
        requirement_id="R1", case_id="PG-1", kind=RequirementKind.PERFORMANCE,
        obligation=RequirementObligation.MANDATORY, attribute="Design pressure",
        operator=ComparisonOperator.GTE, raw_text="minimum 16 bar",
        target_numeric=Decimal(16), uom="BAR",
    )
    return Requirement(**{**defaults, **kwargs})


def test_silence_is_never_compliance():
    status, _ = _requirement().evaluate(offered_text=None)
    assert status == ComplianceStatus.NOT_ADDRESSED


def test_minimum_bound_is_enforced():
    assert _requirement().evaluate(offered_text="20 bar", offered_numeric=Decimal(20), offered_uom="BAR")[0] == ComplianceStatus.COMPLIANT
    assert _requirement().evaluate(offered_text="12 bar", offered_numeric=Decimal(12), offered_uom="BAR")[0] == ComplianceStatus.NON_COMPLIANT


def test_unit_conversion_before_comparison():
    """240 psi satisfies a 16 bar minimum; 200 psi does not.

    16 bar is 232.06 psi, so a 232 psi offer is genuinely 0.03% short. The
    comparison is exact enough to catch that, which is the reason conversion
    happens in Decimal against a rate table rather than by eye.
    """
    requirement = _requirement()
    ok, _ = requirement.evaluate(
        offered_text="240 psi", offered_numeric=Decimal(240), offered_uom="PSI"
    )
    marginal, _ = requirement.evaluate(
        offered_text="232 psi", offered_numeric=Decimal(232), offered_uom="PSI"
    )
    low, _ = requirement.evaluate(
        offered_text="200 psi", offered_numeric=Decimal(200), offered_uom="PSI"
    )
    assert ok == ComplianceStatus.COMPLIANT
    assert marginal == ComplianceStatus.NON_COMPLIANT
    assert low == ComplianceStatus.NON_COMPLIANT


def test_tolerance_band():
    requirement = _requirement(
        operator=ComparisonOperator.TOLERANCE, target_numeric=Decimal("3.2"),
        tolerance_plus=Decimal("0.2"), tolerance_minus=Decimal("0.2"), uom="MM",
        attribute="Wall thickness",
    )
    inside, _ = requirement.evaluate(offered_text="3.05 mm", offered_numeric=Decimal("3.05"), offered_uom="MM")
    outside, _ = requirement.evaluate(offered_text="2.8 mm", offered_numeric=Decimal("2.8"), offered_uom="MM")
    assert inside == ComplianceStatus.COMPLIANT
    assert outside == ComplianceStatus.NON_COMPLIANT


def test_assertion_without_a_value_is_unverifiable_not_compliant():
    status, _ = _requirement().evaluate(offered_text="Fully compliant")
    assert status != ComplianceStatus.COMPLIANT


def test_refusal_phrasing_is_read_as_non_compliant():
    requirement = _requirement(
        operator=ComparisonOperator.BOOLEAN, target_value="yes", target_numeric=None,
        uom="", attribute="ISO 9001 certification",
    )
    status, _ = requirement.evaluate(offered_text="No - not available")
    assert status == ComplianceStatus.NON_COMPLIANT


def test_same_unit_needs_no_conversion_even_when_unregistered():
    """An engineering unit the converter does not know must still compare."""
    requirement = _requirement(uom="WIDGETS", target_numeric=Decimal(10))
    status, _ = requirement.evaluate(
        offered_text="12 WIDGETS", offered_numeric=Decimal(12), offered_uom="WIDGETS"
    )
    assert status == ComplianceStatus.COMPLIANT


# ── qualification ────────────────────────────────────────────────────────────

def test_mandatory_failure_disqualifies_and_deviation_acceptance_rescues():
    requirements = [_requirement()]
    failing = ComplianceAssessment(
        requirement_id="R1", supplier_id="V1", status=ComplianceStatus.DEVIATION,
        offered_value="12 bar", rationale="below minimum",
    )
    qualified, blockers = ProcurementPolicy.qualifies_technically([failing], requirements)
    assert not qualified and blockers

    failing.deviation_accepted = True
    qualified, blockers = ProcurementPolicy.qualifies_technically([failing], requirements)
    assert qualified and not blockers


def test_unassessed_mandatory_requirement_blocks():
    qualified, blockers = ProcurementPolicy.qualifies_technically([], [_requirement()])
    assert not qualified and blockers


# ── requisition validation ───────────────────────────────────────────────────

def test_requisition_rejects_bad_input():
    pr = PurchaseRequisition(
        pr_number="", plant_code="", requester="",
        lines=[PurchaseRequisitionLine(line_number=1, material_code="", quantity=Decimal(0), uom="")],
    )
    errors = pr.validate()
    assert any("PR number" in e for e in errors)
    assert any("Plant code" in e for e in errors)
    assert any("quantity" in e for e in errors)


def test_valid_requisition_passes():
    pr = PurchaseRequisition(
        pr_number="PR-1001", plant_code="1000", requester="Dana",
        lines=[
            PurchaseRequisitionLine(
                line_number=10, material_code="VAL-1023", quantity=Decimal(250), uom="EA",
                required_date=datetime.now(UTC) + timedelta(days=30),
            )
        ],
    )
    assert pr.is_valid, pr.validate()


def test_approval_requires_a_reason():
    approval = Approval(
        approval_id="A1", case_id="PG-1", approval_type=ApprovalType.AWARD,
        decision=ApprovalDecision.APPROVED, actor_id="jordan.head", reason="ok",
    )
    approval.reason = "x"
    with pytest.raises(ValidationError):
        approval.validate()


def test_automated_po_creation_is_off_by_default():
    assert not ProcurementPolicy().may_create_po_in_erp().allowed


def test_external_email_is_gated_by_default():
    policy = ProcurementPolicy(allow_automated_email_send=False)
    assert not policy.may_transmit_email(is_external=True).allowed
    assert policy.may_transmit_email(is_external=False).allowed
