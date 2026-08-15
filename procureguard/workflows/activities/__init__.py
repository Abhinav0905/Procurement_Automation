"""Temporal activities.

Each activity is a thin adapter over one application service. The pipeline logic
lives in `procureguard.application`; the activity's job is transaction scope,
serialisation and idempotency - nothing else.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from temporalio import activity

from procureguard.application.bid_ranking import BidRankingService
from procureguard.application.commercial_normalization import CommercialNormalizationService
from procureguard.application.document_ingestion import DocumentIngestionService
from procureguard.application.history_service import HistoricalProcurementService, PriceBenchmark
from procureguard.application.mailroom import MailroomService
from procureguard.application.material_master import MaterialMasterValidationService
from procureguard.application.negotiation import NegotiationService
from procureguard.application.po_recommendation import PoRecommendationService
from procureguard.application.quotation_ingestion import QuotationIngestionService
from procureguard.application.requirements import RequirementExtractionService
from procureguard.application.rfq_service import RfqGenerationService
from procureguard.application.supplier_shortlist import SupplierShortlistService
from procureguard.application.technical_comparison import TechnicalComparisonService
from procureguard.domain.enums import (
    CaseState,
    CommunicationType,
    RfqInvitationStatus,
    RfqStatus,
)
from procureguard.observability import logger

from .base import activity_handler, with_context

log = logger(__name__)


# ═══════════════════════════════════════════════════════════ intake & validation

@activity.defn(name="validate_pr_activity")
@activity_handler("validate_pr")
async def validate_pr_activity(args: dict[str, Any]) -> dict[str, Any]:
    """Stages 1-2: confirm the requisition can actually be sourced."""
    with with_context(tenant_id=args.get("tenant_id", ""), correlation_id=args.get("correlation_id", "")) as ctx:
        case = ctx.repos.cases.require(args["case_id"])
        if case.state == CaseState.RECEIVED:
            case.transition(CaseState.VALIDATING_PR, actor="SYSTEM", reason="Automated validation")
            ctx.repos.cases.save(case)

        pr = ctx.repos.requisitions.get(case.pr_number)
        if pr is None:
            return {"valid": False, "errors": [f"Requisition {case.pr_number} not found"]}

        report = MaterialMasterValidationService(ctx).validate(pr, case_id=case.case_id)
        for resolution in report.resolutions:
            ctx.repos.requisitions.update_line_validation(
                pr.pr_number,
                resolution.line_number,
                status=resolution.status,
                messages=resolution.messages + resolution.blocking_messages,
                resolved_material_code=resolution.resolved_material_code,
                resolution_method=resolution.resolution_method,
                resolution_confidence=resolution.confidence,
                normalized_uom=resolution.normalized_uom,
            )

        needs_engineering = report.needs_engineering or not report.is_valid
        return {
            "valid": report.is_valid,
            "needs_engineering": needs_engineering,
            "blocking_messages": report.blocking_messages,
            "material_codes": [
                r.resolved_material_code for r in report.resolutions if r.resolved_material_code
            ],
            "report": report.to_dict(),
        }


@activity.defn(name="ingest_document_activity")
@activity_handler("ingest_document")
async def ingest_document_activity(args: dict[str, Any]) -> dict[str, Any]:
    """Stage 4: firewall, extract and index an engineering document."""
    import base64

    with with_context(tenant_id=args.get("tenant_id", "")) as ctx:
        content = base64.b64decode(args["content_b64"])
        result = DocumentIngestionService(ctx).ingest(
            content=content,
            filename=args["filename"],
            case_id=args.get("case_id", ""),
            document_type=args.get("document_type", "TECHNICAL_SPECIFICATION"),
            authority=args.get("authority", "ENGINEERING"),
            media_type=args.get("media_type", ""),
        )
        return result.to_dict()


@activity.defn(name="extract_requirements_activity")
@activity_handler("extract_requirements")
async def extract_requirements_activity(args: dict[str, Any]) -> dict[str, Any]:
    """Stage 5."""
    with with_context(tenant_id=args.get("tenant_id", "")) as ctx:
        result = RequirementExtractionService(ctx).extract_for_case(
            args["case_id"], use_model=args.get("use_model", True)
        )
        return {
            "total": result.total,
            "mandatory": result.mandatory_count,
            "documents_read": result.documents_read,
            "warnings": result.warnings,
        }


# ══════════════════════════════════════════════════════════════════ sourcing

@activity.defn(name="build_benchmarks_activity")
@activity_handler("build_benchmarks")
async def build_benchmarks_activity(args: dict[str, Any]) -> dict[str, Any]:
    """Stage 3: historical price benchmark for every requisition line."""
    with with_context(tenant_id=args.get("tenant_id", "")) as ctx:
        case = ctx.repos.cases.require(args["case_id"])
        pr = ctx.repos.requisitions.get(case.pr_number)
        if pr is None:
            return {"benchmarks": {}}

        service = HistoricalProcurementService(ctx)
        benchmarks: dict[str, Any] = {}
        estimated_total = Decimal(0)
        for line in pr.lines:
            material_code = line.material_code
            if not material_code:
                continue
            benchmark = service.build_benchmark(
                material_code,
                requested_quantity=line.quantity,
                requested_uom=line.uom,
                plant_code=line.plant_code or pr.plant_code,
                case_id=case.case_id,
            )
            benchmarks[material_code] = benchmark.to_dict()
            extended = benchmark.extended_benchmark()
            if extended:
                estimated_total += extended

        if estimated_total:
            case.estimated_value_base = estimated_total
            ctx.repos.cases.save(case, estimated_value_base=estimated_total)
        return {"benchmarks": benchmarks, "estimated_value_base": str(estimated_total)}


@activity.defn(name="build_shortlist_activity")
@activity_handler("build_shortlist")
async def build_shortlist_activity(args: dict[str, Any]) -> dict[str, Any]:
    """Stage 6."""
    with with_context(tenant_id=args.get("tenant_id", "")) as ctx:
        case = ctx.repos.cases.require(args["case_id"])
        pr = ctx.repos.requisitions.get(case.pr_number)
        if pr is None or not pr.lines:
            return {"selected_vendor_ids": [], "warnings": ["No requisition lines"]}

        service = SupplierShortlistService(ctx)
        primary = pr.lines[0]
        benchmark_payload = (args.get("benchmarks") or {}).get(primary.material_code)
        benchmark = _benchmark_from_dict(benchmark_payload) if benchmark_payload else None

        result = service.build(
            case_id=case.case_id,
            material_code=primary.material_code,
            plant_code=primary.plant_code or pr.plant_code,
            benchmark=benchmark,
            requirement_text=primary.description,
            preferred_vendor_id=primary.preferred_vendor_id,
        )
        if case.state == CaseState.VALIDATING_PR:
            case.transition(CaseState.SOURCING_STRATEGY, actor="SYSTEM", reason="Shortlist prepared")
            ctx.repos.cases.save(case)
        return {
            "selected_vendor_ids": result.selected_vendor_ids,
            "is_single_source": result.is_single_source,
            "candidate_count": len(result.candidates),
            "warnings": result.warnings,
        }


@activity.defn(name="prepare_rfq_activity")
@activity_handler("prepare_rfq")
async def prepare_rfq_activity(args: dict[str, Any]) -> dict[str, Any]:
    """Stage 7."""
    with with_context(tenant_id=args.get("tenant_id", "")) as ctx:
        case = ctx.repos.cases.require(args["case_id"])
        if case.state == CaseState.SOURCING_STRATEGY:
            case.transition(CaseState.READY_FOR_RFQ, actor="SYSTEM", reason="RFQ preparation")
            ctx.repos.cases.save(case)

        benchmarks = {
            code: _benchmark_from_dict(payload)
            for code, payload in (args.get("benchmarks") or {}).items()
        }
        result = RfqGenerationService(ctx).build(
            case_id=case.case_id,
            benchmarks=benchmarks,
            response_days=args.get("response_days"),
        )
        return result.to_dict()


@activity.defn(name="send_rfq_invitations_activity")
@activity_handler("send_rfq_invitations")
async def send_rfq_invitations_activity(args: dict[str, Any]) -> dict[str, Any]:
    """Stage 8 outbound: transmit (or hold) the RFQ to each invited supplier."""
    with with_context(tenant_id=args.get("tenant_id", "")) as ctx:
        case = ctx.repos.cases.require(args["case_id"])
        rfq = ctx.repos.rfqs.get(args["rfq_id"])
        if rfq is None:
            return {"sent": 0, "outcomes": [], "error": "RFQ not found"}

        rfq_service = RfqGenerationService(ctx)
        mailroom = MailroomService(ctx)
        body = rfq_service.render_rfq_document(rfq.id)
        template = rfq_service.render_response_template(rfq.id)

        from procureguard.ports.services import EmailAttachment

        outcomes: list[dict[str, Any]] = []
        for invitation in ctx.repos.rfqs.list_invitations(rfq.id):
            outcome = mailroom.send(
                case_id=case.case_id,
                vendor_id=invitation.vendor_id,
                communication_type=CommunicationType.RFQ_INVITATION,
                to=[invitation.contact_email],
                subject=f"{rfq.rfq_number} - Request for quotation - response due {rfq.response_deadline.date()}",
                body_text=body,
                idempotency_key=f"rfq-invite:{rfq.id}:{invitation.vendor_id}",
                rfq_id=rfq.id,
                invitation_id=invitation.id,
                reply_to=invitation.reply_to_address,
                thread_token=invitation.response_token,
                attachments=[
                    EmailAttachment(
                        filename=f"{rfq.rfq_number}-response-template.csv",
                        content=template.encode("utf-8"),
                        media_type="text/csv",
                    )
                ],
            )
            if outcome.transmitted:
                invitation.status = RfqInvitationStatus.SENT.value
                invitation.sent_at = datetime.now(UTC)
                invitation.last_contact_at = invitation.sent_at
                invitation.thread_message_id = outcome.external_message_id
            else:
                invitation.status = RfqInvitationStatus.QUEUED.value
            outcomes.append({**outcome.to_dict(), "vendor_id": invitation.vendor_id})

        transmitted = sum(1 for o in outcomes if o["transmitted"])
        if transmitted or outcomes:
            rfq.status = RfqStatus.ISSUED.value
            rfq.issue_date = rfq.issue_date or datetime.now(UTC)
            if case.state == CaseState.READY_FOR_RFQ:
                case.transition(
                    CaseState.WAITING_FOR_QUOTES, actor="SYSTEM", reason=f"RFQ {rfq.rfq_number} issued"
                )
                ctx.repos.cases.save(case)
        return {
            "sent": transmitted,
            "held_for_release": len(outcomes) - transmitted,
            "supplier_ids": [o["vendor_id"] for o in outcomes],
            "response_deadline": rfq.response_deadline.isoformat(),
            "outcomes": outcomes,
        }


@activity.defn(name="supplier_reminder_activity")
@activity_handler("supplier_reminder")
async def supplier_reminder_activity(args: dict[str, Any]) -> dict[str, Any]:
    """Policy-gated RFQ chase. Refuses to exceed the configured reminder limit."""
    with with_context(tenant_id=args.get("tenant_id", "")) as ctx:
        case = ctx.repos.cases.require(args["case_id"])
        vendor_id = args["supplier_id"]
        rfq = ctx.repos.rfqs.latest_for_case(case.case_id)
        invitation = ctx.repos.rfqs.find_invitation(case.case_id, vendor_id)
        if rfq is None or invitation is None:
            return {"status": "NO_INVITATION"}
        if invitation.status in (
            RfqInvitationStatus.QUOTED.value,
            RfqInvitationStatus.DECLINED.value,
        ):
            return {"status": "ALREADY_RESPONDED"}

        last_contact = ctx.repos.communications.last_outbound_to_vendor(case.case_id, vendor_id)
        decision = ctx.policy.may_send_reminder(
            case,
            vendor_id,
            last_contact_at=last_contact.sent_at if last_contact else invitation.sent_at,
        )
        if not decision.allowed:
            log.info("reminder_suppressed", case_id=case.case_id, vendor_id=vendor_id, reason=decision.reason)
            return {"status": "POLICY_GATED", "reason": decision.reason}

        attempt = case.register_reminder(vendor_id, ctx.policy.max_rfq_reminders)
        ctx.repos.cases.save(case)

        outcome = MailroomService(ctx).send(
            case_id=case.case_id,
            vendor_id=vendor_id,
            communication_type=CommunicationType.RFQ_REMINDER,
            to=[invitation.contact_email],
            subject=f"Reminder {attempt} - {rfq.rfq_number} response due {rfq.response_deadline.date()}",
            body_text=(
                f"Dear {invitation.contact_name or invitation.vendor_name},\n\n"
                f"We have not yet received your quotation against {rfq.rfq_number}, which "
                f"closes on {rfq.response_deadline.strftime('%d %B %Y at %H:%M UTC')}.\n\n"
                f"If you intend to quote, please send your offer by the deadline. If you do "
                f"not, a short 'no bid' reply is appreciated and keeps you on our invitation "
                f"list for future enquiries.\n\n"
                f"Kind regards,\n{ctx.settings.email_from_name}"
            ),
            idempotency_key=f"rfq-reminder:{rfq.id}:{vendor_id}:{attempt}",
            rfq_id=rfq.id,
            invitation_id=invitation.id,
            reply_to=invitation.reply_to_address,
            thread_token=invitation.response_token,
            in_reply_to=invitation.thread_message_id,
        )
        invitation.reminders_sent = attempt
        invitation.last_contact_at = datetime.now(UTC)
        return {"status": "SENT" if outcome.transmitted else "HELD", "attempt": attempt}


@activity.defn(name="poll_inbound_mail_activity")
@activity_handler("poll_inbound_mail")
async def poll_inbound_mail_activity(args: dict[str, Any]) -> dict[str, Any]:
    """Stage 8 inbound + stage 9: pull replies and turn them into quotations."""
    with with_context(tenant_id=args.get("tenant_id", "")) as ctx:
        mailroom = MailroomService(ctx)
        ingestion = QuotationIngestionService(ctx)
        processed: list[dict[str, Any]] = []
        for outcome in mailroom.poll(limit=args.get("limit", 50)):
            entry = outcome.to_dict()
            if (
                outcome.case_id
                and not outcome.quarantined
                and outcome.classification
                in (
                    CommunicationType.QUOTATION_RECEIPT.value,
                    CommunicationType.NEGOTIATION_RESPONSE.value,
                )
            ):
                try:
                    result = ingestion.ingest_from_communication(outcome.communication_id)
                    entry["quotation"] = result.to_dict()
                except Exception as exc:
                    entry["quotation_error"] = str(exc)[:300]
                    log.error(
                        "quotation_ingest_failed",
                        communication_id=outcome.communication_id,
                        detail=str(exc)[:300],
                    )
            processed.append(entry)
        return {"processed": len(processed), "results": processed}


@activity.defn(name="check_quote_status_activity")
@activity_handler("check_quote_status")
async def check_quote_status_activity(args: dict[str, Any]) -> dict[str, Any]:
    """Who has answered, who has not, and may evaluation begin?"""
    with with_context(tenant_id=args.get("tenant_id", "")) as ctx:
        case = ctx.repos.cases.require(args["case_id"])
        rfq = ctx.repos.rfqs.latest_for_case(case.case_id)
        if rfq is None:
            return {"quotes_received": 0, "may_evaluate": False, "pending_supplier_ids": []}

        invitations = ctx.repos.rfqs.list_invitations(rfq.id)
        received = ctx.repos.quotations.count_received(case.case_id)
        deadline_passed = datetime.now(UTC) >= rfq.response_deadline
        decision = ctx.policy.may_open_technical_evaluation(
            case, quotes_received=received, deadline_passed=deadline_passed
        )
        return {
            "quotes_received": received,
            "invited": len(invitations),
            "deadline_passed": deadline_passed,
            "may_evaluate": decision.allowed,
            "reason": decision.reason,
            "pending_supplier_ids": [
                i.vendor_id
                for i in invitations
                if i.status
                not in (
                    RfqInvitationStatus.QUOTED.value,
                    RfqInvitationStatus.DECLINED.value,
                    RfqInvitationStatus.DISQUALIFIED.value,
                )
            ],
        }


@activity.defn(name="close_rfq_activity")
@activity_handler("close_rfq")
async def close_rfq_activity(args: dict[str, Any]) -> dict[str, Any]:
    with with_context(tenant_id=args.get("tenant_id", "")) as ctx:
        rfq = ctx.repos.rfqs.latest_for_case(args["case_id"])
        if rfq is None:
            return {"closed": False}
        ctx.repos.rfqs.close(rfq)
        return {"closed": True, "rfq_number": rfq.rfq_number}


# ═══════════════════════════════════════════════════════════════ evaluation

@activity.defn(name="technical_evaluation_activity")
@activity_handler("technical_evaluation")
async def technical_evaluation_activity(args: dict[str, Any]) -> dict[str, Any]:
    """Stage 10. Runs entirely against sealed bids."""
    with with_context(tenant_id=args.get("tenant_id", "")) as ctx:
        case = ctx.repos.cases.require(args["case_id"])
        if case.state == CaseState.WAITING_FOR_QUOTES:
            case.transition(
                CaseState.TECHNICAL_EVALUATION, actor="SYSTEM", reason="Quote window closed"
            )
            ctx.repos.cases.save(case)

        matrix = TechnicalComparisonService(ctx).evaluate_case(
            case.case_id, use_model=args.get("use_model", True)
        )

        case = ctx.repos.cases.require(args["case_id"])
        if case.state == CaseState.TECHNICAL_EVALUATION:
            case.transition(
                CaseState.WAITING_FOR_TECHNICAL_APPROVAL,
                actor="SYSTEM",
                reason="Technical comparison ready for engineering review",
            )
            ctx.repos.cases.save(case)
        return {
            "status": "RECOMMENDATION_CREATED",
            "evaluated": len(matrix.evaluations),
            "qualified_vendor_ids": matrix.qualified_vendor_ids,
            "warnings": matrix.warnings,
        }


@activity.defn(name="unseal_bids_activity")
@activity_handler("unseal_bids")
async def unseal_bids_activity(args: dict[str, Any]) -> dict[str, Any]:
    """Open the commercial envelopes. Requires a recorded human approval."""
    with with_context(tenant_id=args.get("tenant_id", ""), actor_id=args.get("actor_id", "SYSTEM")) as ctx:
        count = QuotationIngestionService(ctx).unseal_case(
            args["case_id"], actor_id=args["actor_id"]
        )
        return {"unsealed": count}


@activity.defn(name="commercial_evaluation_activity")
@activity_handler("commercial_evaluation")
async def commercial_evaluation_activity(args: dict[str, Any]) -> dict[str, Any]:
    """Stages 12-13: normalise then rank."""
    with with_context(tenant_id=args.get("tenant_id", "")) as ctx:
        case = ctx.repos.cases.require(args["case_id"])
        benchmarks = {
            code: _benchmark_from_dict(payload)
            for code, payload in (args.get("benchmarks") or {}).items()
        }
        normalization = CommercialNormalizationService(ctx).normalize_case(
            case.case_id, negotiation_round=args.get("negotiation_round")
        )
        ranking = BidRankingService(ctx).rank(
            case_id=case.case_id, normalization=normalization, benchmarks=benchmarks
        )
        return {
            "status": "EVALUATED",
            "ranking_run_id": ranking.ranking_run_id,
            "l1_vendor_id": ranking.l1.vendor_id if ranking.l1 else None,
            "l1_total_base": str(ranking.l1.total_base) if ranking.l1 else None,
            "ranked": len(ranking.bids),
            "disqualified": len(ranking.disqualified),
            "split_award_beneficial": bool(ranking.split_award.get("beneficial")),
            "warnings": ranking.warnings,
        }


# ═════════════════════════════════════════════════════════════ negotiation

@activity.defn(name="negotiation_recommendation_activity")
@activity_handler("negotiation_recommendation")
async def negotiation_recommendation_activity(args: dict[str, Any]) -> dict[str, Any]:
    """Stage 14: draft a round. Held for human release before it is sent."""
    with with_context(tenant_id=args.get("tenant_id", "")) as ctx:
        case = ctx.repos.cases.require(args["case_id"])
        decision = ctx.policy.may_open_negotiation(case)
        if not decision.allowed:
            return {"status": "SKIPPED", "reason": decision.reason}

        rankings = ctx.repos.rankings.latest_run(case.case_id)
        if not rankings:
            return {"status": "SKIPPED", "reason": "No ranking run available"}

        normalization = CommercialNormalizationService(ctx).normalize_case(case.case_id)
        ranking = BidRankingService(ctx).rank(
            case_id=case.case_id,
            normalization=normalization,
            benchmarks={
                code: _benchmark_from_dict(payload)
                for code, payload in (args.get("benchmarks") or {}).items()
            },
        )
        if not ranking.bids:
            return {"status": "SKIPPED", "reason": "No qualified bid to negotiate"}

        if case.state == CaseState.COMMERCIAL_EVALUATION:
            case.transition(CaseState.NEGOTIATION, actor="SYSTEM", reason="Negotiation round opened")
            ctx.repos.cases.save(case)

        plan = NegotiationService(ctx).plan_round(
            case_id=case.case_id,
            ranking=ranking,
            benchmarks={
                code: _benchmark_from_dict(payload)
                for code, payload in (args.get("benchmarks") or {}).items()
            },
            invite_top_n=args.get("invite_top_n", 3),
            response_days=args.get("response_days", 5),
        )
        return {
            "status": "RECOMMENDATION_CREATED",
            "round_id": plan.round_id,
            "round_number": plan.round_number,
            "strategy": plan.strategy,
            "target_total_base": str(plan.target_total_base),
            "supplier_ids": [a.vendor_id for a in plan.asks],
            "warnings": plan.warnings,
        }


@activity.defn(name="send_negotiation_round_activity")
@activity_handler("send_negotiation_round")
async def send_negotiation_round_activity(args: dict[str, Any]) -> dict[str, Any]:
    with with_context(tenant_id=args.get("tenant_id", ""), actor_id=args.get("actor_id", "SYSTEM")) as ctx:
        outcomes = NegotiationService(ctx).send_round(
            case_id=args["case_id"],
            round_id=args["round_id"],
            approval_id=args["approval_id"],
        )
        return {"outcomes": outcomes, "sent": sum(1 for o in outcomes if o["transmitted"])}


@activity.defn(name="close_negotiation_round_activity")
@activity_handler("close_negotiation_round")
async def close_negotiation_round_activity(args: dict[str, Any]) -> dict[str, Any]:
    with with_context(tenant_id=args.get("tenant_id", "")) as ctx:
        summary = NegotiationService(ctx).close_round(
            case_id=args["case_id"], round_id=args["round_id"]
        )
        case = ctx.repos.cases.require(args["case_id"])
        if case.state == CaseState.NEGOTIATION:
            case.transition(
                CaseState.WAITING_FOR_AWARD_APPROVAL,
                actor="SYSTEM",
                reason=f"Negotiation round {summary['round_number']} closed",
            )
            ctx.repos.cases.save(case)
        return summary


# ═════════════════════════════════════════════════════════════════════ award

@activity.defn(name="po_recommendation_activity")
@activity_handler("po_recommendation")
async def po_recommendation_activity(args: dict[str, Any]) -> dict[str, Any]:
    """Stage 15. Produces a draft only; never creates a PO in the ERP."""
    with with_context(tenant_id=args.get("tenant_id", "")) as ctx:
        case = ctx.repos.cases.require(args["case_id"])
        normalization = CommercialNormalizationService(ctx).normalize_case(case.case_id)
        benchmarks = {
            code: _benchmark_from_dict(payload)
            for code, payload in (args.get("benchmarks") or {}).items()
        }
        ranking = BidRankingService(ctx).rank(
            case_id=case.case_id, normalization=normalization, benchmarks=benchmarks
        )
        result = PoRecommendationService(ctx).recommend(
            case_id=case.case_id,
            ranking=ranking,
            benchmarks=benchmarks,
            award_vendor_id=args.get("award_vendor_id", "") or case.awarded_supplier_id,
        )
        if case.state == CaseState.WAITING_FOR_AWARD_APPROVAL:
            case.transition(
                CaseState.PO_RECOMMENDATION, actor="SYSTEM", reason="Award approved"
            )
            case.awarded_value_base = result.total_amount_base
            case.savings_base = result.savings_vs_benchmark_base
            ctx.repos.cases.save(
                case,
                awarded_value_base=result.total_amount_base,
                savings_base=result.savings_vs_benchmark_base,
            )
        return {**result.to_dict(), "status": "DRAFT_ONLY"}


@activity.defn(name="schedule_delivery_reminders_activity")
@activity_handler("schedule_delivery_reminders")
async def schedule_delivery_reminders_activity(args: dict[str, Any]) -> dict[str, Any]:
    """Set up the delivery follow-up schedule after award."""
    with with_context(tenant_id=args.get("tenant_id", "")) as ctx:
        case = ctx.repos.cases.require(args["case_id"])
        recommendation = ctx.repos.po_recommendations.latest_for_case(case.case_id)
        if recommendation is None:
            return {"scheduled": 0}

        scheduled = 0
        for line in recommendation.lines:
            if not line.delivery_date:
                continue
            # Chase before the date, then escalate after it.
            for offset_days, reminder_type in (
                (-14, "PRE_DELIVERY_CONFIRMATION"),
                (-3, "PRE_DELIVERY_REMINDER"),
                (1, "OVERDUE_FOLLOW_UP"),
                (7, "OVERDUE_ESCALATION"),
            ):
                due = line.delivery_date + timedelta(days=offset_days)
                if due <= datetime.now(UTC) and offset_days < 0:
                    continue
                ctx.repos.reminders.schedule(
                    case_id=case.case_id,
                    reminder_type=reminder_type,
                    due_at=due,
                    vendor_id=recommendation.vendor_id,
                    subject_ref=f"{recommendation.recommendation_number}:{line.line_number}",
                    payload={
                        "material_code": line.material_code,
                        "quantity": str(line.quantity),
                        "uom": line.uom,
                        "delivery_date": line.delivery_date.isoformat(),
                    },
                )
                scheduled += 1
        return {"scheduled": scheduled, "vendor_id": recommendation.vendor_id}


@activity.defn(name="send_delivery_reminder_activity")
@activity_handler("send_delivery_reminder")
async def send_delivery_reminder_activity(args: dict[str, Any]) -> dict[str, Any]:
    """Periodic delivery chase against the awarded supplier."""
    with with_context(tenant_id=args.get("tenant_id", "")) as ctx:
        due = ctx.repos.reminders.due(limit=args.get("limit", 50))
        sent = 0
        for reminder in due:
            if reminder.case_id != args.get("case_id", reminder.case_id):
                continue
            vendor = ctx.repos.vendors.get(reminder.vendor_id)
            if vendor is None:
                ctx.repos.reminders.mark_sent(reminder.id)
                continue
            email, contact_name = ctx.repos.vendors.primary_rfq_email(vendor)
            payload = reminder.payload or {}
            escalation = reminder.reminder_type in (
                "OVERDUE_FOLLOW_UP",
                "OVERDUE_ESCALATION",
            )
            outcome = MailroomService(ctx).send(
                case_id=reminder.case_id,
                vendor_id=reminder.vendor_id,
                communication_type=(
                    CommunicationType.OVERDUE_ESCALATION
                    if escalation
                    else CommunicationType.DELIVERY_REMINDER
                ),
                to=[email],
                subject=(
                    f"{'OVERDUE - ' if escalation else ''}Delivery confirmation required - "
                    f"{reminder.subject_ref}"
                ),
                body_text=(
                    f"Dear {contact_name or vendor.name},\n\n"
                    + (
                        "Our records show the following delivery is overdue. Please confirm "
                        "the revised despatch date by return.\n\n"
                        if escalation
                        else "Please confirm that the following delivery remains on schedule.\n\n"
                    )
                    + f"Reference: {reminder.subject_ref}\n"
                    f"Material: {payload.get('material_code', '')}\n"
                    f"Quantity: {payload.get('quantity', '')} {payload.get('uom', '')}\n"
                    f"Agreed delivery date: {str(payload.get('delivery_date', ''))[:10]}\n\n"
                    f"Kind regards,\n{ctx.settings.email_from_name}"
                ),
                idempotency_key=f"delivery-reminder:{reminder.id}",
            )
            ctx.repos.reminders.mark_sent(reminder.id, escalated=escalation)
            if outcome.transmitted:
                sent += 1
        return {"due": len(due), "sent": sent}


@activity.defn(name="finalize_case_activity")
@activity_handler("finalize_case")
async def finalize_case_activity(args: dict[str, Any]) -> dict[str, Any]:
    with with_context(tenant_id=args.get("tenant_id", "")) as ctx:
        case = ctx.repos.cases.require(args["case_id"])
        target = CaseState(args.get("target_state", CaseState.COMPLETED.value))
        if case.state != target and case.can_transition(target):
            case.transition(target, actor="SYSTEM", reason=args.get("reason", "Workflow finished"))
            ctx.repos.cases.save(case)
        return {"case_id": case.case_id, "state": str(case.state)}


@activity.defn(name="record_workflow_handle_activity")
@activity_handler("record_workflow_handle")
async def record_workflow_handle_activity(args: dict[str, Any]) -> dict[str, Any]:
    with with_context(tenant_id=args.get("tenant_id", "")) as ctx:
        row = ctx.repos.cases.get_model(args["case_id"])
        if row is None:
            return {"recorded": False}
        row.workflow_id = args.get("workflow_id", "")
        row.workflow_run_id = args.get("workflow_run_id", "")
        return {"recorded": True}


def _benchmark_from_dict(payload: dict[str, Any] | None) -> PriceBenchmark | None:
    """Rehydrate a benchmark that crossed the workflow/activity boundary."""
    if not payload:
        return None
    benchmark = PriceBenchmark(
        material_code=payload.get("material_code", ""),
        base_currency=payload.get("base_currency", "USD"),
        base_uom=payload.get("base_uom", "EA"),
        requested_quantity=Decimal(str(payload.get("requested_quantity") or 0)),
        has_history=bool(payload.get("has_history")),
        order_count=int(payload.get("order_count") or 0),
    )
    for field_name in (
        "last_unit_price", "min_unit_price", "max_unit_price", "median_unit_price",
        "p25_unit_price", "p75_unit_price", "weighted_avg_unit_price",
        "quantity_adjusted_price", "should_cost", "target_price",
        "active_info_record_price", "standard_price", "price_trend_pct_per_year",
        "volatility_pct",
    ):
        value = payload.get(field_name)
        if value is not None:
            setattr(benchmark, field_name, Decimal(str(value)))
    benchmark.should_cost_basis = payload.get("should_cost_basis", "")
    benchmark.notes = list(payload.get("notes", []))
    return benchmark


ALL_ACTIVITIES = [
    validate_pr_activity,
    ingest_document_activity,
    extract_requirements_activity,
    build_benchmarks_activity,
    build_shortlist_activity,
    prepare_rfq_activity,
    send_rfq_invitations_activity,
    supplier_reminder_activity,
    poll_inbound_mail_activity,
    check_quote_status_activity,
    close_rfq_activity,
    technical_evaluation_activity,
    unseal_bids_activity,
    commercial_evaluation_activity,
    negotiation_recommendation_activity,
    send_negotiation_round_activity,
    close_negotiation_round_activity,
    po_recommendation_activity,
    schedule_delivery_reminders_activity,
    send_delivery_reminder_activity,
    finalize_case_activity,
    record_workflow_handle_activity,
]
