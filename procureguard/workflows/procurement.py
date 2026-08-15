"""The durable procurement workflow.

Temporal owns time, retries and legal ordering. The workflow itself contains no
business rules beyond sequencing - it decides *when* an activity may run, and
waits, sometimes for months, at the four points where a human must act:

    engineering input -> RFQ release -> technical approval -> award approval

Everything the workflow does between those points is resumable. If the worker
dies mid-RFQ, the process continues from the same step on a new worker with no
duplicated emails, because the sends are idempotency-keyed in the activity.

Activities are invoked by string name so that no application, database or
network code is imported into the workflow sandbox.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

# Transient infrastructure failures retry; a policy refusal or a validation
# error is a decision, not a blip, and must surface immediately.
STANDARD_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=5,
    non_retryable_error_types=[
        "POLICY_VIOLATION",
        "VALIDATION_FAILED",
        "NOT_FOUND",
        "UNSAFE_TRANSITION",
        "DOMAIN_INVARIANT",
        "SEALED_BID_LOCKED",
        "FORBIDDEN",
    ],
)

QUICK_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)


@dataclass
class ProcurementWorkflowInput:
    case_id: str
    pr_artifact_uri: str = ""
    tenant_id: str = ""
    correlation_id: str = ""
    quote_window_days: int = 10
    reminder_interval_hours: int = 72
    max_reminders: int = 2
    enable_negotiation: bool = True
    poll_interval_minutes: int = 15
    auto_release_rfq: bool = False


@dataclass
class ProcurementWorkflowState:
    case_id: str
    stage: str = "RECEIVED"
    rfq_id: str = ""
    rfq_released: bool = False
    rfq_release_approval_id: str = ""
    technical_approved: bool = False
    technical_approver: str = ""
    award_approved: bool = False
    award_supplier_id: str = ""
    negotiation_round_id: str = ""
    negotiation_approval_id: str = ""
    negotiation_approved: bool = False
    negotiation_rounds_run: int = 0
    supplier_responses: dict[str, bool] = field(default_factory=dict)
    engineering_ready: bool = False
    cancelled: bool = False
    cancellation_reason: str = ""
    failed: bool = False
    failure_reason: str = ""
    last_error: str = ""
    quotes_received: int = 0
    l1_vendor_id: str = ""
    l1_total_base: str = ""
    po_recommendation_id: str = ""


@workflow.defn(name="ProcurementWorkflow")
class ProcurementWorkflow:
    def __init__(self) -> None:
        self.state: ProcurementWorkflowState | None = None
        self._input: ProcurementWorkflowInput | None = None
        self._benchmarks: dict[str, Any] = {}

    # ------------------------------------------------------------------- run
    @workflow.run
    async def run(self, inp: ProcurementWorkflowInput) -> dict[str, Any]:
        self._input = inp
        self.state = ProcurementWorkflowState(case_id=inp.case_id)
        base = {
            "case_id": inp.case_id,
            "tenant_id": inp.tenant_id,
            "correlation_id": inp.correlation_id,
        }

        await self._call("record_workflow_handle_activity", {
            **base,
            "workflow_id": workflow.info().workflow_id,
            "workflow_run_id": workflow.info().run_id,
        }, retry=QUICK_RETRY)

        try:
            # ── 1-2. Requisition validation and material master ──────────────
            self._set_stage("VALIDATING_PR")
            validation = await self._call("validate_pr_activity", base)

            if validation.get("needs_engineering"):
                self._set_stage("WAITING_FOR_ENGINEERING")
                await workflow.wait_condition(
                    lambda: bool(self.state and (self.state.engineering_ready or self.state.cancelled))
                )
                if self._stop():
                    return self._result()
                # Re-validate: engineering may have changed the material or spec.
                validation = await self._call("validate_pr_activity", base)
                if validation.get("needs_engineering"):
                    return await self._fail(
                        "Requisition still cannot be sourced after engineering input: "
                        + "; ".join(validation.get("blocking_messages", []))[:500]
                    )

            # ── 3-6. Benchmarks, requirements, shortlist ─────────────────────
            self._set_stage("SOURCING_STRATEGY")
            await self._call("extract_requirements_activity", base)
            benchmark_result = await self._call("build_benchmarks_activity", base)
            self._benchmarks = benchmark_result.get("benchmarks", {})

            shortlist = await self._call(
                "build_shortlist_activity", {**base, "benchmarks": self._benchmarks}
            )
            if not shortlist.get("selected_vendor_ids"):
                return await self._fail(
                    "No supplier could be shortlisted; manual sourcing is required"
                )

            # ── 7. RFQ package ───────────────────────────────────────────────
            self._set_stage("READY_FOR_RFQ")
            rfq = await self._call(
                "prepare_rfq_activity",
                {
                    **base,
                    "benchmarks": self._benchmarks,
                    "response_days": inp.quote_window_days,
                },
            )
            self.state.rfq_id = rfq["rfq_id"]

            # Human gate: an RFQ is a commitment of the company's name.
            if not inp.auto_release_rfq:
                self._set_stage("WAITING_FOR_RFQ_RELEASE")
                await workflow.wait_condition(
                    lambda: bool(self.state and (self.state.rfq_released or self.state.cancelled))
                )
                if self._stop():
                    return self._result()

            # ── 8. Issue and chase ───────────────────────────────────────────
            sent = await self._call(
                "send_rfq_invitations_activity", {**base, "rfq_id": self.state.rfq_id}
            )
            for supplier_id in sent.get("supplier_ids", []):
                self.state.supplier_responses.setdefault(supplier_id, False)
            self._set_stage("WAITING_FOR_QUOTES")

            await self._collect_quotes(base, inp)
            if self._stop():
                return self._result()
            await self._call("close_rfq_activity", base, retry=QUICK_RETRY)

            # ── 10. Technical evaluation, against sealed bids ────────────────
            self._set_stage("TECHNICAL_EVALUATION")
            evaluation = await self._call(
                "technical_evaluation_activity", base, timeout=timedelta(minutes=30)
            )
            if not evaluation.get("qualified_vendor_ids"):
                workflow.logger.warning(
                    "No technically qualified supplier; awaiting human direction"
                )

            # ── 11. Human gate: technical approval unseals commercial data ───
            self._set_stage("WAITING_FOR_TECHNICAL_APPROVAL")
            await workflow.wait_condition(
                lambda: bool(self.state and (self.state.technical_approved or self.state.cancelled))
            )
            if self._stop():
                return self._result()

            await self._call(
                "unseal_bids_activity",
                {**base, "actor_id": self.state.technical_approver or "UNKNOWN"},
            )

            # ── 12-13. Commercial normalisation and L1/L2/L3 ─────────────────
            self._set_stage("COMMERCIAL_EVALUATION")
            commercial = await self._call(
                "commercial_evaluation_activity",
                {**base, "benchmarks": self._benchmarks},
                timeout=timedelta(minutes=20),
            )
            self.state.l1_vendor_id = commercial.get("l1_vendor_id") or ""
            self.state.l1_total_base = commercial.get("l1_total_base") or ""

            # ── 14. Negotiation rounds, each human-released ──────────────────
            if inp.enable_negotiation:
                await self._run_negotiation(base, inp)
                if self._stop():
                    return self._result()

            # ── Human gate: award ────────────────────────────────────────────
            self._set_stage("WAITING_FOR_AWARD_APPROVAL")
            await workflow.wait_condition(
                lambda: bool(self.state and (self.state.award_approved or self.state.cancelled))
            )
            if self._stop():
                return self._result()

            # ── 15. PO recommendation ────────────────────────────────────────
            self._set_stage("PO_RECOMMENDATION")
            recommendation = await self._call(
                "po_recommendation_activity",
                {**base, "award_vendor_id": self.state.award_supplier_id},
                timeout=timedelta(minutes=15),
            )
            self.state.po_recommendation_id = recommendation.get("recommendation_id", "")

            # ── Delivery follow-up ───────────────────────────────────────────
            await self._call("schedule_delivery_reminders_activity", base, retry=QUICK_RETRY)
            self._set_stage("EXPEDITING")
            await self._expedite(base)

            await self._call(
                "finalize_case_activity", {**base, "target_state": "COMPLETED"}, retry=QUICK_RETRY
            )
            self._set_stage("COMPLETED")
            return {
                "case_id": inp.case_id,
                "state": "COMPLETED",
                "po_recommendation": recommendation,
                "l1_vendor_id": self.state.l1_vendor_id,
                "negotiation_rounds": self.state.negotiation_rounds_run,
            }

        except ApplicationError as exc:
            return await self._fail(f"{exc.type}: {exc}")
        except ActivityError as exc:
            return await self._fail(str(exc.cause or exc)[:500])

    # ------------------------------------------------------------- sub-phases
    async def _collect_quotes(
        self, base: dict[str, Any], inp: ProcurementWorkflowInput
    ) -> None:
        """Poll for replies, chase non-responders, and stop at the deadline.

        The loop is bounded by the quote window, so a supplier who never answers
        cannot hold the case open indefinitely.
        """
        assert self.state is not None
        deadline = workflow.now() + timedelta(days=inp.quote_window_days)
        last_reminder = workflow.now()
        reminders_sent: dict[str, int] = {}

        while workflow.now() < deadline:
            if self.state.cancelled:
                return

            poll = await self._call("poll_inbound_mail_activity", base, retry=QUICK_RETRY)
            if poll.get("processed"):
                status = await self._call("check_quote_status_activity", base, retry=QUICK_RETRY)
                self.state.quotes_received = status.get("quotes_received", 0)
                for supplier_id in self.state.supplier_responses:
                    if supplier_id not in status.get("pending_supplier_ids", []):
                        self.state.supplier_responses[supplier_id] = True
                if status.get("may_evaluate") and all(self.state.supplier_responses.values()):
                    return

            elapsed = workflow.now() - last_reminder
            if elapsed >= timedelta(hours=inp.reminder_interval_hours):
                last_reminder = workflow.now()
                status = await self._call("check_quote_status_activity", base, retry=QUICK_RETRY)
                for supplier_id in status.get("pending_supplier_ids", []):
                    if reminders_sent.get(supplier_id, 0) >= inp.max_reminders:
                        continue
                    outcome = await self._call(
                        "supplier_reminder_activity", {**base, "supplier_id": supplier_id}
                    )
                    if outcome.get("status") in ("SENT", "HELD"):
                        reminders_sent[supplier_id] = reminders_sent.get(supplier_id, 0) + 1

            await workflow.wait_condition(
                lambda: bool(
                    self.state
                    and (self.state.cancelled or all(self.state.supplier_responses.values()))
                ),
                timeout=timedelta(minutes=inp.poll_interval_minutes),
            )
            if self.state.cancelled or all(self.state.supplier_responses.values()):
                break

        # Deadline reached: proceed with whatever arrived, if policy permits.
        status = await self._call("check_quote_status_activity", base, retry=QUICK_RETRY)
        self.state.quotes_received = status.get("quotes_received", 0)
        if not status.get("may_evaluate"):
            workflow.logger.warning(
                "Quote window closed without enough responses: %s", status.get("reason")
            )

    async def _run_negotiation(
        self, base: dict[str, Any], inp: ProcurementWorkflowInput
    ) -> None:
        """Draft a round, wait for release, wait for replies, close, re-rank."""
        assert self.state is not None

        plan = await self._call(
            "negotiation_recommendation_activity",
            {**base, "benchmarks": self._benchmarks},
            timeout=timedelta(minutes=15),
        )
        if plan.get("status") != "RECOMMENDATION_CREATED":
            workflow.logger.info("Negotiation skipped: %s", plan.get("reason"))
            return

        self.state.negotiation_round_id = plan["round_id"]
        self.state.negotiation_approved = False
        self._set_stage("WAITING_FOR_NEGOTIATION_APPROVAL")

        # A price ask goes out under a named human's authority, never the agent's.
        approved = await self._wait_for(
            lambda: bool(self.state and self.state.negotiation_approved),
            timeout=timedelta(days=7),
        )
        if self._stop():
            return
        if not approved:
            workflow.logger.info("Negotiation round not approved within 7 days; skipping")
            return

        self._set_stage("NEGOTIATION")
        await self._call(
            "send_negotiation_round_activity",
            {
                **base,
                "round_id": self.state.negotiation_round_id,
                "approval_id": self.state.negotiation_approval_id,
                "actor_id": self.state.technical_approver or "SYSTEM",
            },
        )

        response_deadline = workflow.now() + timedelta(days=7)
        while workflow.now() < response_deadline and not self.state.cancelled:
            await self._call("poll_inbound_mail_activity", base, retry=QUICK_RETRY)
            await workflow.wait_condition(
                lambda: bool(self.state and self.state.cancelled),
                timeout=timedelta(minutes=inp.poll_interval_minutes),
            )
            if self.state.cancelled:
                return

        await self._call(
            "close_negotiation_round_activity",
            {**base, "round_id": self.state.negotiation_round_id},
        )
        self.state.negotiation_rounds_run += 1

        commercial = await self._call(
            "commercial_evaluation_activity",
            {**base, "benchmarks": self._benchmarks},
            timeout=timedelta(minutes=20),
        )
        self.state.l1_vendor_id = commercial.get("l1_vendor_id") or self.state.l1_vendor_id
        self.state.l1_total_base = commercial.get("l1_total_base") or self.state.l1_total_base

    async def _expedite(self, base: dict[str, Any]) -> None:
        """Send delivery reminders on schedule until every one has fired."""
        assert self.state is not None
        for _ in range(60):  # bounded: a case does not expedite forever
            if self.state.cancelled:
                return
            result = await self._call(
                "send_delivery_reminder_activity", base, retry=QUICK_RETRY
            )
            if not result.get("due"):
                return
            await workflow.wait_condition(
                lambda: bool(self.state and self.state.cancelled), timeout=timedelta(days=1)
            )

    # ---------------------------------------------------------------- helpers
    async def _call(
        self,
        name: str,
        args: dict[str, Any],
        *,
        timeout: timedelta | None = None,
        retry: RetryPolicy | None = None,
    ) -> dict[str, Any]:
        return await workflow.execute_activity(
            name,
            args,
            start_to_close_timeout=timeout or timedelta(minutes=10),
            retry_policy=retry or STANDARD_RETRY,
            heartbeat_timeout=timedelta(minutes=2) if (timeout or timedelta()) > timedelta(minutes=5) else None,
        )

    async def _wait_for(self, predicate: Any, *, timeout: timedelta) -> bool:
        try:
            await workflow.wait_condition(
                lambda: predicate() or bool(self.state and self.state.cancelled), timeout=timeout
            )
        except TimeoutError:
            return False
        return predicate()

    def _set_stage(self, stage: str) -> None:
        if self.state:
            self.state.stage = stage
            workflow.logger.info("stage=%s", stage)

    def _stop(self) -> bool:
        return bool(self.state and (self.state.cancelled or self.state.failed))

    def _result(self) -> dict[str, Any]:
        assert self.state is not None
        return {
            "case_id": self.state.case_id,
            "state": "CANCELLED" if self.state.cancelled else self.state.stage,
            "reason": self.state.cancellation_reason or self.state.failure_reason,
        }

    async def _fail(self, reason: str) -> dict[str, Any]:
        assert self.state is not None
        self.state.failed = True
        self.state.failure_reason = reason
        self._set_stage("FAILED")
        try:
            await self._call(
                "finalize_case_activity",
                {
                    "case_id": self.state.case_id,
                    "target_state": "FAILED",
                    "reason": reason,
                },
                retry=QUICK_RETRY,
            )
        except Exception:  # noqa: BLE001 - the case is already failing
            workflow.logger.exception("Could not record FAILED state")
        workflow.logger.error("workflow_failed reason=%s", reason)
        return {"case_id": self.state.case_id, "state": "FAILED", "reason": reason}

    # ---------------------------------------------------------------- signals
    @workflow.signal
    async def engineering_information_received(self, note: str = "") -> None:
        if self.state:
            self.state.engineering_ready = True
            workflow.logger.info("engineering input received: %s", note[:200])

    @workflow.signal
    async def rfq_released(self, approval_id: str = "") -> None:
        if self.state:
            self.state.rfq_released = True
            self.state.rfq_release_approval_id = approval_id

    @workflow.signal
    async def supplier_response_received(self, supplier_id: str) -> None:
        if self.state:
            self.state.supplier_responses[supplier_id] = True

    @workflow.signal
    async def technical_approval_received(self, actor_id: str = "") -> None:
        if self.state:
            self.state.technical_approved = True
            self.state.technical_approver = actor_id

    @workflow.signal
    async def negotiation_approved(self, approval_id: str = "") -> None:
        if self.state:
            self.state.negotiation_approved = True
            self.state.negotiation_approval_id = approval_id

    @workflow.signal
    async def award_approval_received(self, supplier_id: str = "") -> None:
        if self.state:
            self.state.award_approved = True
            self.state.award_supplier_id = supplier_id

    @workflow.signal
    async def cancel(self, reason: str = "") -> None:
        if self.state:
            self.state.cancelled = True
            self.state.cancellation_reason = reason

    # ---------------------------------------------------------------- queries
    @workflow.query
    def get_state(self) -> ProcurementWorkflowState | None:
        return self.state

    @workflow.query
    def get_stage(self) -> str:
        return self.state.stage if self.state else "UNKNOWN"

    @workflow.query
    def pending_suppliers(self) -> list[str]:
        if not self.state:
            return []
        return [s for s, responded in self.state.supplier_responses.items() if not responded]
