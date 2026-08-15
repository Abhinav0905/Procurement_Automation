"""Stage 14 - negotiation with versioned rounds.

A negotiation is a chain of immutable rounds. Each round records the baseline it
started from, what was asked of each supplier and why, what came back, and what
it saved. Nothing is overwritten, so "why did we end up at this price?" is
answerable months later by reading the chain.

Two guardrails are absolute:

* A round cannot open before technical approval, and cannot exceed the
  configured round limit.
* The letter to each supplier is drafted, then held for human release. The agent
  never sends a price ask on its own authority, and never reveals a competitor's
  identity or price.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from procureguard.application.bid_ranking import RankingResult
from procureguard.application.history_service import PriceBenchmark
from procureguard.domain.enums import (
    ApprovalType,
    CommunicationType,
    DecisionType,
    NegotiationRoundStatus,
)
from procureguard.domain.errors import PolicyViolationError
from procureguard.infrastructure.factory import ServiceContext
from procureguard.infrastructure.llm.prompts import (
    NEGOTIATION_SCHEMA,
    NEGOTIATION_STRATEGY_SYSTEM,
    trusted_block,
)
from procureguard.observability import logger

log = logger(__name__)

ZERO = Decimal(0)

# Ceiling on any single round's price ask.
MAX_TARGET_REDUCTION_PCT = Decimal(30)


@dataclass(slots=True)
class SupplierAsk:
    vendor_id: str
    vendor_name: str
    baseline_quotation_id: str
    current_total_base: Decimal
    target_total_base: Decimal
    target_reduction_pct: Decimal
    leverage_points: list[str] = field(default_factory=list)
    non_price_asks: list[str] = field(default_factory=list)
    message_body: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "vendor_id": self.vendor_id,
            "vendor_name": self.vendor_name,
            "current_total_base": str(self.current_total_base),
            "target_total_base": str(self.target_total_base),
            "target_reduction_pct": str(self.target_reduction_pct),
            "leverage_points": self.leverage_points,
            "non_price_asks": self.non_price_asks,
        }


@dataclass(slots=True)
class NegotiationPlan:
    case_id: str
    round_id: str
    round_number: int
    strategy: str
    rationale: str
    baseline_total_base: Decimal
    target_total_base: Decimal
    asks: list[SupplierAsk] = field(default_factory=list)
    deadline: datetime | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "round_id": self.round_id,
            "round_number": self.round_number,
            "strategy": self.strategy,
            "rationale": self.rationale,
            "baseline_total_base": str(self.baseline_total_base),
            "target_total_base": str(self.target_total_base),
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "asks": [a.to_dict() for a in self.asks],
            "warnings": self.warnings,
        }


class NegotiationService:
    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------ plan
    def plan_round(
        self,
        *,
        case_id: str,
        ranking: RankingResult,
        benchmarks: dict[str, PriceBenchmark] | None = None,
        invite_top_n: int = 3,
        response_days: int = 5,
        use_model: bool = True,
    ) -> NegotiationPlan:
        case = self.ctx.repos.cases.require(case_id)
        decision = self.ctx.policy.may_open_negotiation(case)
        decision.raise_if_denied()

        if not ranking.bids:
            raise PolicyViolationError(
                "There is no qualified bid to negotiate against", case_id=case_id
            )

        round_number = case.open_negotiation_round(self.ctx.policy.max_negotiation_rounds)
        self.ctx.repos.cases.save(case)

        benchmarks = benchmarks or {}
        baseline_total = ranking.bids[0].total_base
        strategy, strategy_reason = self._choose_strategy(ranking, benchmarks)
        deadline = datetime.now(UTC) + timedelta(days=response_days)

        round_row = self.ctx.repos.negotiations.create_round(
            case_id=case_id,
            round_number=round_number,
            status=NegotiationRoundStatus.DRAFT.value,
            strategy=strategy,
            rationale=strategy_reason,
            baseline_ranking_run_id=ranking.ranking_run_id,
            baseline_total_base=baseline_total,
            opened_by=self.ctx.actor_id,
            deadline=deadline,
        )

        plan = NegotiationPlan(
            case_id=case_id,
            round_id=round_row.id,
            round_number=round_number,
            strategy=strategy,
            rationale=strategy_reason,
            baseline_total_base=baseline_total,
            target_total_base=ZERO,
            deadline=deadline,
        )

        invited = ranking.bids[: max(1, invite_top_n)]
        for bid in invited:
            ask = self._build_ask(bid, ranking, benchmarks, strategy)
            if use_model:
                self._model_enrich(ask, bid, ranking, benchmarks)
            ask.message_body = self._render_letter(ask, round_number, deadline)
            plan.asks.append(ask)

            self.ctx.repos.negotiations.add_target(
                round_row,
                vendor_id=ask.vendor_id,
                vendor_name=ask.vendor_name,
                baseline_quotation_id=ask.baseline_quotation_id,
                current_total_base=ask.current_total_base,
                target_total_base=ask.target_total_base,
                target_reduction_pct=ask.target_reduction_pct,
                non_price_asks=ask.non_price_asks,
                leverage_points=ask.leverage_points,
                message_body=ask.message_body,
                status="DRAFT",
            )

        plan.target_total_base = min((a.target_total_base for a in plan.asks), default=ZERO)
        round_row.target_total_base = plan.target_total_base
        round_row.status = NegotiationRoundStatus.PENDING_APPROVAL.value
        self.ctx.session.flush()

        if len(invited) == 1:
            plan.warnings.append(
                "Only one supplier is being negotiated with; without a credible alternative "
                "the leverage is limited to benchmark data"
            )

        self.ctx.repos.decisions.record(
            case_id=case_id,
            decision_type=DecisionType.NEGOTIATION_STRATEGY.value,
            recommendation=plan.to_dict(),
            rationale=strategy_reason,
            confidence=Decimal("0.75"),
            model_metadata={"engine": "deterministic-negotiation-planner-v1", "strategy": strategy},
            evidence=[
                {
                    "evidence_type": "BID_RANKING",
                    "evidence_id": ranking.ranking_run_id,
                    "role": "BASELINE",
                }
            ],
        )
        self.ctx.audit(
            entity_type="NEGOTIATION_ROUND",
            entity_id=round_row.id,
            case_id=case_id,
            action="NEGOTIATION_ROUND_DRAFTED",
            after_state={
                "round": round_number,
                "strategy": strategy,
                "suppliers": [a.vendor_id for a in plan.asks],
                "target_total_base": str(plan.target_total_base),
            },
        )
        log.info(
            "negotiation_round_drafted",
            case_id=case_id,
            round=round_number,
            strategy=strategy,
            suppliers=len(plan.asks),
        )
        return plan

    # ------------------------------------------------------------------ send
    def send_round(self, *, case_id: str, round_id: str, approval_id: str) -> list[dict[str, Any]]:
        """Transmit an approved round. Requires a recorded NEGOTIATION_SEND approval."""
        from procureguard.application.mailroom import MailroomService

        round_row = self.ctx.repos.negotiations.get_round(round_id)
        if round_row is None:
            raise PolicyViolationError(f"Negotiation round {round_id} not found")

        approvals = self.ctx.repos.approvals.list_for_case(case_id, ApprovalType.NEGOTIATION_SEND)
        if not any(a.approval_id == approval_id and a.is_positive for a in approvals):
            raise PolicyViolationError(
                "A negotiation round can only be sent under a recorded, approved "
                "NEGOTIATION_SEND approval",
                case_id=case_id,
                round_id=round_id,
            )

        mailroom = MailroomService(self.ctx)
        rfq = self.ctx.repos.rfqs.latest_for_case(case_id)
        outcomes: list[dict[str, Any]] = []

        for target in round_row.targets:
            invitation = self.ctx.repos.rfqs.find_invitation(case_id, target.vendor_id)
            vendor = self.ctx.repos.vendors.get(target.vendor_id)
            if vendor is None:
                continue
            email, contact_name = self.ctx.repos.vendors.primary_rfq_email(vendor)
            outcome = mailroom.send(
                case_id=case_id,
                vendor_id=target.vendor_id,
                communication_type=CommunicationType.NEGOTIATION_ROUND,
                to=[email],
                subject=(
                    f"Revised offer requested - {rfq.rfq_number if rfq else case_id} "
                    f"(round {round_row.round_number})"
                ),
                body_text=target.message_body,
                idempotency_key=f"negotiation:{round_id}:{target.vendor_id}",
                rfq_id=rfq.id if rfq else "",
                invitation_id=invitation.id if invitation else "",
                reply_to=invitation.reply_to_address if invitation else "",
                thread_token=invitation.response_token if invitation else "",
                in_reply_to=invitation.thread_message_id if invitation else "",
            )
            target.communication_id = outcome.communication_id
            target.status = "SENT" if outcome.transmitted else "PENDING_APPROVAL"
            outcomes.append({**outcome.to_dict(), "vendor_id": target.vendor_id})

        round_row.status = (
            NegotiationRoundStatus.SENT.value
            if any(o["transmitted"] for o in outcomes)
            else NegotiationRoundStatus.PENDING_APPROVAL.value
        )
        round_row.approval_id = approval_id
        round_row.sent_at = datetime.now(UTC)
        self.ctx.session.flush()

        self.ctx.audit(
            entity_type="NEGOTIATION_ROUND",
            entity_id=round_id,
            case_id=case_id,
            action="NEGOTIATION_ROUND_SENT",
            after_state={"outcomes": outcomes, "approval_id": approval_id},
        )
        return outcomes

    # -------------------------------------------------------------- responses
    def record_response(
        self,
        *,
        case_id: str,
        round_id: str,
        vendor_id: str,
        quotation_id: str,
        achieved_total_base: Decimal | None = None,
    ) -> dict[str, Any]:
        """Attach a revised quotation to its negotiation target.

        `achieved_total_base` lets a caller that has just normalised the round
        pass the figure in rather than making this method normalise it again -
        the same numbers either way, one pass instead of two.
        """
        from procureguard.application.commercial_normalization import (
            CommercialNormalizationService,
        )

        round_row = self.ctx.repos.negotiations.get_round(round_id)
        if round_row is None:
            raise PolicyViolationError(f"Negotiation round {round_id} not found")

        if achieved_total_base is not None:
            achieved = achieved_total_base
        else:
            normalization = CommercialNormalizationService(self.ctx).normalize_case(
                case_id, negotiation_round=round_row.round_number
            )
            vendor_lines = normalization.by_vendor().get(vendor_id, [])
            achieved = sum((line.total_cost_of_ownership_base for line in vendor_lines), ZERO)

        self.ctx.repos.negotiations.record_response(
            round_id, vendor_id, response_quotation_id=quotation_id, achieved_total_base=achieved
        )
        target = self.ctx.repos.negotiations.get_target(round_id, vendor_id)
        saving = (
            Decimal(str(target.current_total_base or 0)) - achieved if target else ZERO
        )
        self.ctx.audit(
            entity_type="NEGOTIATION_TARGET",
            entity_id=f"{round_id}:{vendor_id}",
            case_id=case_id,
            action="NEGOTIATION_RESPONSE_RECEIVED",
            after_state={
                "achieved_total_base": str(achieved),
                "saving_base": str(saving),
            },
        )
        return {
            "vendor_id": vendor_id,
            "achieved_total_base": str(achieved),
            "saving_base": str(saving),
            "target_met": bool(target and achieved <= Decimal(str(target.target_total_base or 0))),
        }

    def close_round(self, *, case_id: str, round_id: str) -> dict[str, Any]:
        round_row = self.ctx.repos.negotiations.get_round(round_id)
        if round_row is None:
            raise PolicyViolationError(f"Negotiation round {round_id} not found")

        achieved = [
            Decimal(str(t.achieved_total_base))
            for t in round_row.targets
            if t.achieved_total_base is not None
        ]
        best = min(achieved) if achieved else None
        self.ctx.repos.negotiations.close_round(round_row, achieved_total_base=best)

        responded = len(achieved)
        summary = {
            "round_number": round_row.round_number,
            "suppliers_asked": len(round_row.targets),
            "suppliers_responded": responded,
            "baseline_total_base": str(round_row.baseline_total_base),
            "achieved_total_base": str(best) if best is not None else None,
            "savings_base": str(round_row.savings_base),
            "savings_pct": str(round_row.savings_pct),
        }
        self.ctx.audit(
            entity_type="NEGOTIATION_ROUND",
            entity_id=round_id,
            case_id=case_id,
            action="NEGOTIATION_ROUND_CLOSED",
            after_state=summary,
        )
        log.info("negotiation_round_closed", case_id=case_id, **summary)
        return summary

    def history(self, case_id: str) -> list[dict[str, Any]]:
        """The full versioned chain, for the award file."""
        out: list[dict[str, Any]] = []
        for round_row in self.ctx.repos.negotiations.list_rounds(case_id):
            out.append(
                {
                    "round_number": round_row.round_number,
                    "status": round_row.status,
                    "strategy": round_row.strategy,
                    "rationale": round_row.rationale,
                    "baseline_total_base": str(round_row.baseline_total_base),
                    "target_total_base": str(round_row.target_total_base),
                    "achieved_total_base": (
                        str(round_row.achieved_total_base)
                        if round_row.achieved_total_base is not None
                        else None
                    ),
                    "savings_base": str(round_row.savings_base),
                    "savings_pct": str(round_row.savings_pct),
                    "sent_at": round_row.sent_at.isoformat() if round_row.sent_at else None,
                    "closed_at": round_row.closed_at.isoformat() if round_row.closed_at else None,
                    "approval_id": round_row.approval_id,
                    "targets": [
                        {
                            "vendor_id": t.vendor_id,
                            "current_total_base": str(t.current_total_base),
                            "target_total_base": str(t.target_total_base),
                            "achieved_total_base": (
                                str(t.achieved_total_base)
                                if t.achieved_total_base is not None
                                else None
                            ),
                            "achieved_reduction_pct": (
                                str(t.achieved_reduction_pct)
                                if t.achieved_reduction_pct is not None
                                else None
                            ),
                            "status": t.status,
                            "leverage_points": t.leverage_points,
                            "non_price_asks": t.non_price_asks,
                        }
                        for t in round_row.targets
                    ],
                }
            )
        return out

    # --------------------------------------------------------------- strategy
    def _choose_strategy(
        self, ranking: RankingResult, benchmarks: dict[str, PriceBenchmark]
    ) -> tuple[str, str]:
        if len(ranking.bids) >= 2:
            spread = ranking.bids[-1].delta_vs_l1_pct
            if spread <= Decimal(5):
                return (
                    "BEST_AND_FINAL",
                    f"Bids are tightly clustered ({spread}% spread), so a simultaneous "
                    f"best-and-final round extracts more than sequential bargaining",
                )
            return (
                "COMPETITIVE_TARGET",
                f"A {spread}% spread across {len(ranking.bids)} qualified bids gives real "
                f"competitive leverage; each supplier is given a target price",
            )
        reference = next(
            (b.benchmark_unit_price for b in benchmarks.values() if b.benchmark_unit_price), None
        )
        if reference is not None:
            return (
                "BENCHMARK_TARGET",
                "Only one qualified bid, so leverage comes from documented historical "
                "prices rather than competition",
            )
        return (
            "COST_JUSTIFICATION",
            "Single bid with no internal price history; ask for a cost breakdown rather "
            "than a blind discount",
        )

    def _build_ask(
        self,
        bid: Any,
        ranking: RankingResult,
        benchmarks: dict[str, PriceBenchmark],
        strategy: str,
    ) -> SupplierAsk:
        settings = self.ctx.settings
        current = bid.total_base
        target_pct = Decimal(str(settings.negotiation_target_savings_pct))

        leverage: list[str] = []
        # Competitive pressure: state that a gap exists, never who or how much.
        if bid.position > 1:
            leverage.append(
                "Your offer is not currently the most competitive received for this enquiry"
            )
            # The reduction needed to reach L1, expressed against this supplier's
            # own price. Using delta_vs_l1_pct directly is wrong: that is a
            # percentage of L1, and for a bid at twice L1 it exceeds 100, which
            # produced a negative - and therefore meaningless - target price.
            leader_total = ranking.bids[0].total_base
            if current > 0 and leader_total > 0:
                gap_of_own = (current - leader_total) / current * Decimal(100)
                target_pct = max(target_pct, gap_of_own + Decimal(2))
        elif len(ranking.bids) > 1:
            leverage.append(
                "Your offer is competitive, but the field is close and price is not yet decided"
            )

        benchmark_note = self._benchmark_leverage(benchmarks)
        if benchmark_note:
            leverage.append(benchmark_note)

        if bid.delta_vs_benchmark_pct is not None and bid.delta_vs_benchmark_pct > Decimal(5):
            leverage.append(
                f"The quoted level is {bid.delta_vs_benchmark_pct}% above what this material "
                f"has historically cost us"
            )
        if bid.partial_offer:
            leverage.append(
                f"You quoted {bid.lines_covered} of {bid.lines_total} lines; quoting the "
                f"complete scope would improve your position"
            )

        # No supplier concedes half their price, and asking for it destroys
        # credibility, so the ask is capped at a level a buyer would actually
        # put in writing.
        target_pct = max(Decimal(0), min(target_pct, MAX_TARGET_REDUCTION_PCT))
        target_total = (current * (Decimal(1) - target_pct / Decimal(100))).quantize(
            Decimal("0.01")
        )
        non_price = self._non_price_asks(bid, strategy)

        return SupplierAsk(
            vendor_id=bid.vendor_id,
            vendor_name=bid.vendor_name,
            baseline_quotation_id=bid.quotation_id,
            current_total_base=current,
            target_total_base=target_total,
            target_reduction_pct=target_pct.quantize(Decimal("0.01")),
            leverage_points=leverage,
            non_price_asks=non_price,
        )

    @staticmethod
    def _benchmark_leverage(benchmarks: dict[str, PriceBenchmark]) -> str:
        for benchmark in benchmarks.values():
            if benchmark.should_cost is not None and benchmark.has_history:
                return (
                    f"We have previously procured this material at "
                    f"{benchmark.should_cost} {benchmark.base_currency} per "
                    f"{benchmark.base_uom} ({benchmark.should_cost_basis})"
                )
        return ""

    @staticmethod
    def _non_price_asks(bid: Any, strategy: str) -> list[str]:
        """Concessions worth real money that suppliers give up more readily."""
        asks = [
            "Extend payment terms to net 60 days",
            "Hold the quoted price firm for 12 months",
            "Confirm delivery within the required date at no expedite charge",
        ]
        if "PARTIAL_OFFER" in " ".join(bid.flags):
            asks.append("Quote the remaining line items")
        if any(flag.startswith("LINE_") and "PRICE_INCREASE" in flag for flag in bid.flags):
            asks.append("Provide a cost breakdown for the lines that increased against last year")
        if strategy == "BEST_AND_FINAL":
            asks.append("Include freight and packing in the unit price on a DAP basis")
        return asks

    def _model_enrich(
        self,
        ask: SupplierAsk,
        bid: Any,
        ranking: RankingResult,
        benchmarks: dict[str, PriceBenchmark],
    ) -> None:
        """Optional model pass to sharpen talking points.

        Deliberately fed only this supplier's own numbers plus our internal
        benchmark - never the competing bids - so a leaked prompt cannot leak a
        competitor's price.
        """
        evidence = {
            "supplier": ask.vendor_id,
            "their_total": str(ask.current_total_base),
            "our_target": str(ask.target_total_base),
            "target_reduction_pct": str(ask.target_reduction_pct),
            "their_position_is_leading": bid.position == 1,
            "historical_benchmarks": [
                {
                    "material_code": b.material_code,
                    "benchmark_unit_price": str(b.benchmark_unit_price),
                    "should_cost": str(b.should_cost),
                    "trend_pct_per_year": str(b.price_trend_pct_per_year),
                }
                for b in benchmarks.values()
                if b.has_history
            ],
            "deterministic_leverage_points": ask.leverage_points,
        }
        try:
            response = self.ctx.model.generate_json(
                system=NEGOTIATION_STRATEGY_SYSTEM,
                prompt=trusted_block(str(evidence), label="NEGOTIATION EVIDENCE"),
                schema=NEGOTIATION_SCHEMA,
                purpose="negotiation_strategy",
            )
        except Exception as exc:
            log.info("negotiation_model_enrichment_skipped", detail=str(exc)[:200])
            return
        payload = response.content if isinstance(response.content, dict) else {}
        for point in payload.get("talking_points", []) or []:
            text = str(point).strip()
            if text and text not in ask.leverage_points:
                ask.leverage_points.append(text[:400])
        for item in payload.get("non_price_asks", []) or []:
            text = str(item).strip()
            if text and text not in ask.non_price_asks:
                ask.non_price_asks.append(text[:200])

    def _render_letter(self, ask: SupplierAsk, round_number: int, deadline: datetime) -> str:
        """Draft the supplier-facing letter.

        Written as a template rather than generated prose: this text is a
        commercial communication that may be quoted back at us, and a template
        is auditable in a way a paraphrase is not.
        """
        leverage = "\n".join(f"  - {point}" for point in ask.leverage_points)
        asks = "\n".join(f"  {index}. {item}" for index, item in enumerate(ask.non_price_asks, 1))
        return (
            f"Dear {ask.vendor_name},\n\n"
            f"Thank you for your quotation. We have completed our technical evaluation and "
            f"your offer has been assessed as technically acceptable.\n\n"
            f"Before we finalise this award, we are inviting a revised offer "
            f"(round {round_number}).\n\n"
            f"Context for our request:\n{leverage or '  - We are seeking your most competitive position'}\n\n"
            f"We are asking you to review your commercial offer with a target improvement of "
            f"{ask.target_reduction_pct}% on your quoted total.\n\n"
            f"We would also welcome movement on the following, which may be worth as much to "
            f"us as price:\n{asks}\n\n"
            f"Please send your revised offer by "
            f"{deadline.strftime('%d %B %Y at %H:%M UTC')}, quoting the same reference and "
            f"keeping your line numbering unchanged so we can compare like for like.\n\n"
            f"To be clear about process: this is a request for a revised offer, not an award. "
            f"No order is placed until a purchase order is issued in writing.\n\n"
            f"Kind regards,\n"
            f"{self.ctx.settings.email_from_name}"
        )
