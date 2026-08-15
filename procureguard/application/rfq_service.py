"""Stage 7 - RFQ generation.

Builds the RFQ document, its line items, its terms, and a per-supplier response
template. The template matters more than it looks: a structured form that names
every requirement is what makes the returned quotations comparable, and it is
the difference between "supplier did not mention hardness" and "supplier ticked
non-compliant on hardness".

Two things are deliberately never in a supplier-facing RFQ: the internal target
price, and the identity of the other invited suppliers.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from procureguard.application.history_service import PriceBenchmark
from procureguard.domain.enums import (
    DecisionType,
    DocumentAuthority,
    DocumentType,
    Incoterm,
    RfqInvitationStatus,
    RfqStatus,
    TrustState,
)
from procureguard.domain.errors import PolicyViolationError, ValidationError
from procureguard.infrastructure.email.mailer import make_response_token, reply_to_address
from procureguard.infrastructure.factory import ServiceContext
from procureguard.infrastructure.storage.object_store import content_key
from procureguard.observability import logger

log = logger(__name__)


@dataclass(slots=True)
class RfqBuildResult:
    rfq_id: str
    rfq_number: str
    status: str
    line_count: int
    invitation_count: int
    response_deadline: datetime
    document_version_id: str
    warnings: list[str] = field(default_factory=list)
    requires_approvals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rfq_id": self.rfq_id,
            "rfq_number": self.rfq_number,
            "status": self.status,
            "line_count": self.line_count,
            "invitation_count": self.invitation_count,
            "response_deadline": self.response_deadline.isoformat(),
            "document_version_id": self.document_version_id,
            "warnings": self.warnings,
            "requires_approvals": self.requires_approvals,
        }


class RfqGenerationService:
    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx

    def build(
        self,
        *,
        case_id: str,
        benchmarks: dict[str, PriceBenchmark] | None = None,
        response_days: int | None = None,
        required_incoterm: str = "",
    ) -> RfqBuildResult:
        settings = self.ctx.settings
        case = self.ctx.repos.cases.require(case_id)
        pr = self.ctx.repos.requisitions.get(case.pr_number)
        if pr is None:
            raise ValidationError(f"Requisition {case.pr_number} not found", case_id=case_id)

        selected = self.ctx.repos.candidates.list_for_case(case_id, selected_only=True)
        if not selected:
            raise PolicyViolationError(
                "No suppliers are selected; a shortlist must be approved before an RFQ",
                case_id=case_id,
            )
        decision = self.ctx.policy.may_issue_rfq(case, [c.vendor_id for c in selected])
        decision.raise_if_denied()

        requirements = self.ctx.repos.requirements.list_active(case_id)
        plant = self.ctx.repos.materials.get_plant(pr.plant_code)
        deadline = datetime.now(UTC) + timedelta(
            days=response_days or settings.quote_window_days
        )
        benchmarks = benchmarks or {}

        rfq = self.ctx.repos.rfqs.create(
            rfq_number=self.ctx.repos.rfqs.next_number(),
            case_id=case_id,
            revision=1,
            status=RfqStatus.DRAFT.value,
            title=f"RFQ for {pr.pr_number} - {_title_for(pr)}",
            response_deadline=deadline,
            validity_days_required=60,
            delivery_plant=pr.plant_code,
            delivery_address=_plant_address(plant),
            required_incoterm=(required_incoterm or Incoterm.DAP.value),
            required_incoterm_location=(plant.city if plant else pr.plant_code),
            currency_preference=settings.base_currency,
            payment_terms_target="NET 45",
            sealed_bid=settings.sealed_bid_enabled,
            terms_and_conditions=_standard_terms(),
            instructions=_instructions(deadline, settings.base_currency),
            response_token_salt=secrets.token_hex(16),
        )

        for line in pr.lines:
            benchmark = benchmarks.get(line.material_code)
            self.ctx.repos.rfqs.add_line(
                rfq,
                line_number=line.line_number,
                pr_line_number=line.line_number,
                material_code=line.material_code,
                description=_line_description(line, self.ctx),
                quantity=Decimal(str(line.quantity)),
                uom=line.uom,
                required_date=line.required_date,
                # Target and should-cost are stored for internal evaluation and
                # negotiation. They are never rendered into supplier documents.
                target_unit_price_base=benchmark.target_price if benchmark else None,
                should_cost_base=benchmark.should_cost if benchmark else None,
                requirement_ids=[
                    r.id for r in requirements if r.pr_line_number in (line.line_number, 1)
                ],
                quantity_breaks=_quantity_breaks(Decimal(str(line.quantity))),
            )

        for candidate in selected:
            vendor = self.ctx.repos.vendors.get(candidate.vendor_id)
            if vendor is None:
                continue
            email, contact_name = self.ctx.repos.vendors.primary_rfq_email(vendor)
            if not email:
                log.warning("vendor_without_email", vendor_id=vendor.vendor_id)
                continue
            token = make_response_token(
                case_id=case_id, vendor_id=vendor.vendor_id, salt=rfq.response_token_salt
            )
            self.ctx.repos.rfqs.add_invitation(
                rfq,
                vendor_id=vendor.vendor_id,
                vendor_name=vendor.name,
                contact_email=email,
                contact_name=contact_name,
                status=RfqInvitationStatus.DRAFT.value,
                response_token=token,
                reply_to_address=reply_to_address(token, settings.email_reply_to_domain),
            )

        document_text = self.render_rfq_document(rfq.id)
        stored = self.ctx.object_store.put(
            key=content_key(
                prefix="rfq", content=document_text.encode(), extension=".md"
            ),
            body=document_text.encode("utf-8"),
            content_type="text/markdown; charset=utf-8",
            metadata={"case_id": case_id, "rfq_number": rfq.rfq_number},
        )
        document = self.ctx.repos.documents.get_or_create_document(
            logical_name=f"{rfq.rfq_number}.md",
            document_type=DocumentType.RFQ,
            case_id=case_id,
        )
        version, _ = self.ctx.repos.documents.add_version(
            document,
            content=document_text.encode("utf-8"),
            storage_uri=stored.uri,
            media_type="text/markdown; charset=utf-8",
            original_filename=f"{rfq.rfq_number}.md",
            authority=DocumentAuthority.PROCUREMENT,
            trust_state=TrustState.AUTHORITATIVE,
            uploaded_by=self.ctx.actor_id,
        )
        rfq.document_version_id = version.id
        rfq.status = RfqStatus.PENDING_RELEASE_APPROVAL.value
        self.ctx.session.flush()

        invitations = self.ctx.repos.rfqs.list_invitations(rfq.id)
        result = RfqBuildResult(
            rfq_id=rfq.id,
            rfq_number=rfq.rfq_number,
            status=rfq.status,
            line_count=len(pr.lines),
            invitation_count=len(invitations),
            response_deadline=deadline,
            document_version_id=version.id,
            warnings=[decision.reason] if decision.required_approvals else [],
            requires_approvals=[str(a) for a in decision.required_approvals],
        )

        self.ctx.repos.decisions.record(
            case_id=case_id,
            decision_type=DecisionType.RFQ_PACKAGE.value,
            recommendation=result.to_dict(),
            rationale=(
                f"RFQ {rfq.rfq_number} prepared for {len(invitations)} supplier(s) with "
                f"{len(requirements)} technical requirement(s); response due {deadline.date()}"
            ),
            confidence=Decimal("0.9"),
            model_metadata={"engine": "deterministic-rfq-builder-v1"},
            evidence=[
                {"evidence_type": "REQUIREMENT", "evidence_id": r.id, "role": "SUPPORTS"}
                for r in requirements[:25]
            ],
        )
        self.ctx.audit(
            entity_type="RFQ",
            entity_id=rfq.id,
            case_id=case_id,
            action="RFQ_DRAFTED",
            after_state={
                "rfq_number": rfq.rfq_number,
                "suppliers": [i.vendor_id for i in invitations],
                "deadline": deadline.isoformat(),
            },
        )
        log.info(
            "rfq_built",
            case_id=case_id,
            rfq_number=rfq.rfq_number,
            suppliers=len(invitations),
            requirements=len(requirements),
        )
        return result

    # ------------------------------------------------------------- rendering
    def render_rfq_document(self, rfq_id: str, *, for_vendor_id: str = "") -> str:
        """Render the supplier-facing RFQ.

        `for_vendor_id` only personalises the addressee; the commercial content
        is identical for every supplier, which is what makes the bids comparable
        and the process defensible.
        """
        rfq = self.ctx.repos.rfqs.get(rfq_id)
        if rfq is None:
            raise ValidationError(f"RFQ {rfq_id} not found")
        requirements = self.ctx.repos.requirements.list_active(rfq.case_id)
        lines = sorted(rfq.lines, key=lambda line: line.line_number)

        parts: list[str] = [
            f"# Request for Quotation {rfq.rfq_number}",
            "",
            f"**Issued by:** {self.ctx.settings.email_from_name}",
            f"**Reference:** {rfq.rfq_number} (revision {rfq.revision})",
            f"**Response deadline:** {rfq.response_deadline.strftime('%d %B %Y, %H:%M UTC')}",
            f"**Quotation validity required:** {rfq.validity_days_required} days from submission",
            f"**Delivery terms:** {rfq.required_incoterm} {rfq.required_incoterm_location}",
            f"**Delivery address:** {rfq.delivery_address or rfq.delivery_plant}",
            f"**Preferred currency:** {rfq.currency_preference} "
            f"(quotations in other currencies are accepted and will be converted at the "
            f"reference rate on the day of receipt)",
            f"**Target payment terms:** {rfq.payment_terms_target}",
            "",
            "## 1. Scope",
            "",
            "Please quote the following items. Partial quotations are acceptable; state "
            "clearly which line items you are **not** quoting.",
            "",
            "## 2. Line items",
            "",
            "| Line | Material | Description | Quantity | UOM | Required by |",
            "| ---: | --- | --- | ---: | --- | --- |",
        ]
        for line in lines:
            required = line.required_date.date().isoformat() if line.required_date else "ASAP"
            parts.append(
                f"| {line.line_number} | {line.material_code or '-'} | "
                f"{_escape_pipe(line.description)} | {_fmt_qty(line.quantity)} | {line.uom} | "
                f"{required} |"
            )

        for line in lines:
            breaks = line.quantity_breaks or []
            if breaks:
                parts += [
                    "",
                    f"**Line {line.line_number} - price breaks requested.** Please quote a unit "
                    f"price at each of the following quantities: "
                    + ", ".join(f"{_fmt_qty(Decimal(str(b)))} {line.uom}" for b in breaks)
                    + ".",
                ]

        if requirements:
            mandatory = [r for r in requirements if r.obligation == "MANDATORY"]
            desirable = [r for r in requirements if r.obligation == "DESIRABLE"]
            parts += ["", "## 3. Technical requirements", ""]
            parts.append(
                "Confirm compliance with **every** item below. An unanswered item is "
                "treated as non-compliant. Where you deviate, state the offered value and "
                "the reason - a declared deviation can be reviewed by engineering, an "
                "undeclared one cannot."
            )
            parts += ["", "### 3.1 Mandatory", "", "| Ref | Requirement | Comply (Y/N) | Offered value |", "| --- | --- | --- | --- |"]
            for requirement in mandatory:
                parts.append(
                    f"| {requirement.requirement_key} | "
                    f"{_escape_pipe(_render_requirement(requirement))} |  |  |"
                )
            if desirable:
                parts += ["", "### 3.2 Desirable", "", "| Ref | Requirement | Comply (Y/N) | Offered value |", "| --- | --- | --- | --- |"]
                for requirement in desirable:
                    parts.append(
                        f"| {requirement.requirement_key} | "
                        f"{_escape_pipe(_render_requirement(requirement))} |  |  |"
                    )

        parts += [
            "",
            "## 4. Commercial information required",
            "",
            "Provide all of the following. Missing commercial information delays "
            "evaluation and may cause your quotation to be set aside.",
            "",
            "- Unit price per line, stating the price basis (for example: per piece, per 100 pieces, per kg)",
            "- Currency",
            "- Incoterms 2020 term and named place",
            "- Freight, packing, tooling and any other charges, itemised separately",
            "- Payment terms",
            "- Lead time from order confirmation, in calendar days",
            "- Minimum order quantity, if any",
            "- Quotation validity period",
            "- Warranty period",
            "",
            "## 5. Terms and conditions",
            "",
            rfq.terms_and_conditions,
            "",
            "## 6. How to respond",
            "",
            rfq.instructions,
            "",
            "---",
            "",
            f"*This RFQ was prepared by an automated procurement system and released by "
            f"{rfq.released_by or 'a member of the procurement team'}. "
            f"All commercial decisions are made by authorised human buyers.*",
        ]
        return "\n".join(parts)

    def render_response_template(self, rfq_id: str) -> str:
        """A CSV a supplier can fill in and return, which parses cleanly."""
        rfq = self.ctx.repos.rfqs.get(rfq_id)
        if rfq is None:
            raise ValidationError(f"RFQ {rfq_id} not found")
        rows = [
            "line_number,material_code,description,quantity,uom,unit_price,price_per_quantity,"
            "currency,lead_time_days,minimum_order_quantity,notes"
        ]
        for line in sorted(rfq.lines, key=lambda line: line.line_number):
            rows.append(
                f"{line.line_number},{line.material_code},"
                f"\"{line.description.replace(chr(34), chr(39))}\","
                f"{_fmt_qty(line.quantity)},{line.uom},,1,{rfq.currency_preference},,,"
            )
        return "\n".join(rows) + "\n"


# ------------------------------------------------------------------ helpers

def _render_requirement(requirement: Any) -> str:
    unit = f" {requirement.uom}" if requirement.uom else ""
    match requirement.operator:
        case "GTE":
            body = f"minimum {requirement.target_numeric}{unit}"
        case "LTE":
            body = f"maximum {requirement.target_numeric}{unit}"
        case "RANGE":
            body = f"{requirement.lower_numeric} to {requirement.upper_numeric}{unit}"
        case "TOLERANCE":
            body = (
                f"{requirement.target_numeric} +{requirement.tolerance_plus}"
                f"/-{requirement.tolerance_minus}{unit}"
            )
        case "ONE_OF":
            body = "one of: " + ", ".join(str(v) for v in (requirement.allowed_values or []))
        case "BOOLEAN":
            body = "required"
        case "PRESENT":
            body = "state your value"
        case _:
            body = f"{requirement.target_value}{unit}"
    return f"{requirement.attribute}: {body}"


def _line_description(line: Any, ctx: ServiceContext) -> str:
    if line.material_code:
        material = ctx.repos.materials.get(line.material_code)
        if material:
            detail = material.long_description or material.description
            spec = material.drawing_number or material.specification_reference
            return f"{detail}{f' (drawing {spec})' if spec else ''}"
    return line.description or "See attached specification"


def _quantity_breaks(quantity: Decimal) -> list[str]:
    """Ask for a price curve, not a point.

    Knowing the supplier's own break points is what lets a buyer decide whether
    ordering 20% more is cheaper in total - and it is free to ask.
    """
    if quantity <= 1:
        return []
    candidates = [quantity / 2, quantity, quantity * 2]
    return [str(c.quantize(Decimal("0.001")).normalize()) for c in candidates if c >= 1]


def _plant_address(plant: Any) -> str:
    if plant is None:
        return ""
    return ", ".join(filter(None, [plant.name, plant.city, plant.country]))


def _title_for(pr: Any) -> str:
    if not pr.lines:
        return "materials"
    first = pr.lines[0].description or pr.lines[0].material_code
    return f"{first}{f' and {len(pr.lines) - 1} more' if len(pr.lines) > 1 else ''}"[:200]


def _fmt_qty(quantity: Decimal) -> str:
    value = Decimal(str(quantity)).normalize()
    return f"{value:f}"


def _escape_pipe(text: str) -> str:
    return (text or "").replace("|", "/").replace("\n", " ").strip()


def _standard_terms() -> str:
    return (
        "1. This request for quotation is an invitation to treat and does not constitute an "
        "order, a commitment to order, or an offer capable of acceptance.\n"
        "2. Quotation preparation costs are borne by the supplier.\n"
        "3. The buyer is not obliged to accept the lowest or any quotation, and may award by "
        "line item or split an award between suppliers.\n"
        "4. Prices quoted shall remain firm for the stated validity period.\n"
        "5. Quotations and all information exchanged are confidential and shall not be "
        "disclosed to third parties.\n"
        "6. Deliveries shall be accompanied by the documentation stated in the technical "
        "requirements; goods may be rejected where documentation is absent.\n"
        "7. Any change to the supplier's bank or remittance details must be notified through "
        "an independently verified channel and will be confirmed by telephone with a known "
        "contact before any payment is made.\n"
        "8. The buyer's standard purchasing terms apply to any resulting order and prevail "
        "over the supplier's terms unless expressly agreed in writing."
    )


def _instructions(deadline: datetime, currency: str) -> str:
    return (
        f"Reply to this email, keeping the subject line unchanged, on or before "
        f"{deadline.strftime('%d %B %Y at %H:%M UTC')}.\n\n"
        f"Attach your quotation as a PDF or spreadsheet, or complete the attached CSV "
        f"template. Quote in {currency} where possible; other currencies are accepted.\n\n"
        f"Include your quotation reference number and validity period. If you do not intend "
        f"to quote, please reply with 'no bid' and a brief reason - this keeps you on the "
        f"invitation list for future enquiries.\n\n"
        f"Technical questions may be asked by reply at any time before the deadline; answers "
        f"that change the requirement will be issued to all invited suppliers."
    )
