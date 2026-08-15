"""Stage 9 - quotation ingestion.

Turns a supplier reply (body text plus attachments) into a structured quotation,
then seals its commercial content.

Sealing is the point of this stage. Until a human approves the technical
evaluation, the prices are encrypted at rest with a per-bid data key bound to
the case. The agent physically cannot read them, so it cannot - even by
accident, even under a prompt injection - let price influence a technical
judgement. Unsealing is a recorded human act.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from procureguard.domain.enums import (
    QuotationStatus,
    RfqInvitationStatus,
    TrustState,
)
from procureguard.domain.errors import ValidationError
from procureguard.infrastructure.factory import ServiceContext
from procureguard.ingestion.parsers.quotation_parser import ParsedQuotation, QuotationParser
from procureguard.observability import logger
from procureguard.security.crypto import seal_payload, unseal_payload

log = logger(__name__)

# Fields moved into the sealed envelope while the case is technically sealed.
SEALED_HEADER_FIELDS = (
    "currency", "total_amount", "freight_amount", "packing_amount", "tooling_amount",
    "other_charges", "discount_amount", "payment_terms",
)


@dataclass(slots=True)
class QuotationIngestResult:
    quotation_id: str
    case_id: str
    vendor_id: str
    status: str
    revision: int
    sealed: bool
    line_count: int
    declined: bool
    parse_confidence: Decimal
    warnings: list[str] = field(default_factory=list)
    superseded_quotation_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "quotation_id": self.quotation_id,
            "case_id": self.case_id,
            "vendor_id": self.vendor_id,
            "status": self.status,
            "revision": self.revision,
            "sealed": self.sealed,
            "line_count": self.line_count,
            "declined": self.declined,
            "parse_confidence": str(self.parse_confidence),
            "warnings": self.warnings,
            "superseded_quotation_id": self.superseded_quotation_id,
        }


class QuotationIngestionService:
    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx
        self.parser = QuotationParser()

    def ingest_from_communication(
        self, communication_id: str, *, negotiation_round: int | None = None
    ) -> QuotationIngestResult:
        communication = self.ctx.repos.communications.get(communication_id)
        if communication is None:
            raise ValidationError(f"Communication {communication_id} not found")
        if not communication.case_id or not communication.vendor_id:
            raise ValidationError(
                "Communication is not matched to a case and supplier; it cannot become a quotation",
                communication_id=communication_id,
            )

        # Body text plus every attachment's extracted text: suppliers routinely
        # put the prices in the attachment and the terms in the email.
        parts: list[str] = [communication.body_text or ""]
        for ref in communication.attachment_refs or []:
            version_id = ref.get("document_version_id")
            if not version_id:
                continue
            version = self.ctx.repos.documents.get_version(version_id)
            if version is None or version.trust_state == TrustState.QUARANTINED.value:
                continue
            parts.append(self._version_text(version))

        return self.ingest_text(
            case_id=communication.case_id,
            vendor_id=communication.vendor_id,
            text="\n\n".join(p for p in parts if p.strip()),
            source_communication_id=communication.id,
            document_version_id=next(
                (
                    ref.get("document_version_id", "")
                    for ref in (communication.attachment_refs or [])
                    if ref.get("document_version_id")
                ),
                "",
            ),
            negotiation_round=negotiation_round,
            received_at=communication.received_at,
        )

    def ingest_text(
        self,
        *,
        case_id: str,
        vendor_id: str,
        text: str,
        source_communication_id: str = "",
        document_version_id: str = "",
        negotiation_round: int | None = None,
        received_at: datetime | None = None,
        received_via: str = "EMAIL",
    ) -> QuotationIngestResult:
        case = self.ctx.repos.cases.require(case_id)
        vendor = self.ctx.repos.vendors.get(vendor_id)
        rfq = self.ctx.repos.rfqs.latest_for_case(case_id)
        rfq_lines = (
            [
                {
                    "line_number": line.line_number,
                    "material_code": line.material_code,
                    "description": line.description,
                    "quantity": line.quantity,
                    "uom": line.uom,
                }
                for line in sorted(rfq.lines, key=lambda x: x.line_number)
            ]
            if rfq
            else []
        )
        round_number = (
            negotiation_round if negotiation_round is not None else case.negotiation_round
        )

        parsed = self.parser.parse(
            text,
            rfq_lines=rfq_lines,
            default_currency=self.ctx.settings.base_currency,
        )

        previous = self.ctx.repos.quotations.find_by_vendor(
            case_id, vendor_id, negotiation_round=round_number
        )
        should_seal = (
            self.ctx.settings.sealed_bid_enabled
            and not case.commercial_unlocked
            and not parsed.declined
        )

        quotation = self.ctx.repos.quotations.create(
            quotation_number=parsed.quotation_number,
            case_id=case_id,
            rfq_id=rfq.id if rfq else "",
            invitation_id=(
                self.ctx.repos.rfqs.find_invitation(case_id, vendor_id).id
                if rfq and self.ctx.repos.rfqs.find_invitation(case_id, vendor_id)
                else ""
            ),
            vendor_id=vendor_id,
            vendor_name=vendor.name if vendor else "",
            revision=(previous.revision + 1) if previous else 1,
            negotiation_round=round_number,
            status=(
                QuotationStatus.WITHDRAWN.value
                if parsed.declined
                else (QuotationStatus.SEALED.value if should_seal else QuotationStatus.PARSED.value)
            ),
            received_at=received_at or datetime.now(UTC),
            received_via=received_via,
            source_communication_id=source_communication_id,
            document_version_id=document_version_id,
            incoterm=parsed.incoterm,
            incoterm_location=parsed.incoterm_location,
            validity_days=parsed.validity_days,
            valid_until=parsed.valid_until
            or (
                datetime.now(UTC) + timedelta(days=parsed.validity_days)
                if parsed.validity_days
                else None
            ),
            lead_time_days=parsed.lead_time_days,
            warranty_months=parsed.warranty_months,
            minimum_order_quantity=parsed.minimum_order_quantity,
            parse_confidence=parsed.confidence,
            parse_warnings=parsed.warnings,
            raw_extract=parsed.to_dict(),
        )

        commercial = {
            "currency": parsed.currency,
            "total_amount": str(parsed.total_amount) if parsed.total_amount is not None else None,
            "freight_amount": str(parsed.freight_amount),
            "packing_amount": str(parsed.packing_amount),
            "tooling_amount": str(parsed.tooling_amount),
            "other_charges": str(parsed.other_charges),
            "discount_amount": str(parsed.discount_amount),
            "payment_terms": parsed.payment_terms,
            "lines": [
                {
                    "rfq_line_number": line.rfq_line_number,
                    "material_code": line.material_code,
                    "offered_description": line.offered_description,
                    "offered_part_number": line.offered_part_number,
                    "quantity": str(line.quantity) if line.quantity is not None else None,
                    "uom": line.uom,
                    "unit_price": str(line.unit_price) if line.unit_price is not None else None,
                    "price_per_quantity": str(line.price_per_quantity),
                    "currency": line.currency,
                    "line_total": str(line.line_total) if line.line_total is not None else None,
                    "lead_time_days": line.lead_time_days,
                    "minimum_order_quantity": (
                        str(line.minimum_order_quantity)
                        if line.minimum_order_quantity is not None
                        else None
                    ),
                    "notes": line.notes,
                    "is_alternative": line.is_alternative,
                    "quantity_breaks": line.quantity_breaks,
                }
                for line in parsed.lines
            ],
        }

        if should_seal:
            ciphertext, key_reference = seal_payload(
                self.ctx.encryptor,
                commercial,
                case_id=case_id,
                quotation_ref=quotation.id,
            )
            quotation.is_sealed = True
            quotation.sealed_payload = ciphertext
            quotation.sealed_key_id = key_reference
            # Technical content only: nothing priced is written in clear.
            self._write_technical_lines(quotation, parsed)
        else:
            self._apply_commercial(quotation, parsed)
            self._write_all_lines(quotation, parsed)

        if previous is not None:
            self.ctx.repos.quotations.supersede(previous, quotation)

        # A negotiation response is a price revision of an already-evaluated
        # offer, so it inherits the technical verdict from the quotation it
        # replaces. Without this, a re-ranking after negotiation sees
        # technically_qualified = NULL and silently drops suppliers who had
        # already passed - dropping the eventual winner in testing.
        self._inherit_technical_result(quotation, previous, round_number, vendor_id, case_id)

        if rfq:
            invitation = self.ctx.repos.rfqs.find_invitation(case_id, vendor_id)
            if invitation is not None:
                invitation.status = (
                    RfqInvitationStatus.DECLINED.value
                    if parsed.declined
                    else RfqInvitationStatus.QUOTED.value
                )
                invitation.responded_at = datetime.now(UTC)

        # Technical answers keyed by requirement become claims with provenance.
        self._record_claims(case_id, vendor_id, quotation.id, parsed, document_version_id)
        self.ctx.session.flush()

        self.ctx.audit(
            entity_type="QUOTATION",
            entity_id=quotation.id,
            case_id=case_id,
            action="QUOTATION_INGESTED",
            after_state={
                "vendor_id": vendor_id,
                "revision": quotation.revision,
                "sealed": quotation.is_sealed,
                "declined": parsed.declined,
                "lines": len(parsed.lines),
            },
            detail=f"Parsed via {parsed.source_format} at confidence {parsed.confidence}",
        )
        log.info(
            "quotation_ingested",
            case_id=case_id,
            vendor_id=vendor_id,
            revision=quotation.revision,
            sealed=quotation.is_sealed,
            lines=len(parsed.lines),
            declined=parsed.declined,
        )
        return QuotationIngestResult(
            quotation_id=quotation.id,
            case_id=case_id,
            vendor_id=vendor_id,
            status=quotation.status,
            revision=quotation.revision,
            sealed=quotation.is_sealed,
            line_count=len(parsed.lines),
            declined=parsed.declined,
            parse_confidence=parsed.confidence,
            warnings=parsed.warnings,
            superseded_quotation_id=previous.id if previous else "",
        )

    def _inherit_technical_result(
        self,
        quotation: Any,
        previous: Any,
        round_number: int,
        vendor_id: str,
        case_id: str,
    ) -> None:
        """Carry a prior technical verdict onto a price revision.

        Only applies when the earlier offer was technically evaluated. If the
        supplier changed what they are offering, the parsed part numbers differ
        and the verdict is *not* inherited, so the requirement matrix is re-run.
        """
        source = previous
        if source is None and round_number > 0:
            # First response inside a new negotiation round: look back at the
            # supplier's pre-negotiation quotation.
            for candidate_round in range(round_number - 1, -1, -1):
                source = self.ctx.repos.quotations.find_by_vendor(
                    case_id, vendor_id, negotiation_round=candidate_round
                )
                if source is not None:
                    break
        if source is None or source.technically_qualified is None:
            return

        offered_before = {
            (line.rfq_line_number, (line.offered_part_number or "").strip().upper())
            for line in source.lines
        }
        offered_now = {
            (line.rfq_line_number, (line.offered_part_number or "").strip().upper())
            for line in quotation.lines
        }
        if offered_before and offered_now and offered_before != offered_now:
            log.info(
                "technical_result_not_inherited",
                case_id=case_id,
                vendor_id=vendor_id,
                reason="offered part numbers changed; technical re-evaluation required",
            )
            return

        quotation.technically_qualified = source.technically_qualified
        quotation.technical_score = source.technical_score
        quotation.disqualification_reasons = list(source.disqualification_reasons or [])
        quotation.status = (
            QuotationStatus.TECHNICALLY_QUALIFIED.value
            if source.technically_qualified
            else QuotationStatus.TECHNICALLY_DISQUALIFIED.value
        )

    # ------------------------------------------------------------- unsealing
    def unseal_case(self, case_id: str, *, actor_id: str) -> int:
        """Decrypt every sealed bid on a case. Only legal after technical approval."""
        case = self.ctx.repos.cases.require(case_id)
        if not case.commercial_unlocked:
            raise ValidationError(
                "Commercial data cannot be unsealed before the technical approval is recorded",
                case_id=case_id,
            )
        if not actor_id or actor_id == "SYSTEM":
            raise ValidationError("Unsealing must be attributed to a human actor")

        unsealed = 0
        for quotation in self.ctx.repos.quotations.list_for_case(
            case_id, commercial_unlocked=True, latest_only=False
        ):
            if not quotation.is_sealed or not quotation.sealed_payload:
                continue
            payload = unseal_payload(
                self.ctx.encryptor,
                quotation.sealed_payload,
                quotation.sealed_key_id,
                case_id=case_id,
                quotation_ref=quotation.id,
            )
            self._apply_sealed_payload(quotation, payload)
            quotation.is_sealed = False
            quotation.unsealed_at = datetime.now(UTC)
            quotation.unsealed_by = actor_id
            quotation.status = (
                QuotationStatus.TECHNICALLY_QUALIFIED.value
                if quotation.technically_qualified
                else quotation.status
            )
            unsealed += 1

        self.ctx.session.flush()
        self.ctx.audit(
            entity_type="SOURCING_CASE",
            entity_id=case_id,
            case_id=case_id,
            action="COMMERCIAL_BIDS_UNSEALED",
            actor_id=actor_id,
            after_state={"quotations_unsealed": unsealed},
            detail="Sealed commercial envelopes opened following technical approval",
        )
        log.info("bids_unsealed", case_id=case_id, count=unsealed, actor_id=actor_id)
        return unsealed

    # ---------------------------------------------------------------- helpers
    def _write_technical_lines(self, quotation: Any, parsed: ParsedQuotation) -> None:
        """Persist line identity and technical content, but no prices."""
        for line in parsed.lines:
            self.ctx.repos.quotations.add_line(
                quotation,
                rfq_line_number=line.rfq_line_number,
                material_code=line.material_code,
                offered_description=line.offered_description,
                offered_part_number=line.offered_part_number,
                quantity=line.quantity or Decimal(0),
                uom=line.uom or "EA",
                unit_price=Decimal(0),
                price_per_quantity=Decimal(1),
                currency="XXX",  # ISO 4217 "no currency"; a sealed placeholder
                line_total=Decimal(0),
                lead_time_days=line.lead_time_days,
                is_alternative=line.is_alternative,
                technical_attributes=line.technical_attributes,
                notes=line.notes,
            )

    def _write_all_lines(self, quotation: Any, parsed: ParsedQuotation) -> None:
        for line in parsed.lines:
            self.ctx.repos.quotations.add_line(
                quotation,
                rfq_line_number=line.rfq_line_number,
                material_code=line.material_code,
                offered_description=line.offered_description,
                offered_part_number=line.offered_part_number,
                quantity=line.quantity or Decimal(0),
                uom=line.uom or "EA",
                unit_price=line.unit_price or Decimal(0),
                price_per_quantity=line.price_per_quantity or Decimal(1),
                currency=line.currency or parsed.currency,
                line_total=line.line_total or Decimal(0),
                lead_time_days=line.lead_time_days,
                minimum_order_quantity=line.minimum_order_quantity,
                is_alternative=line.is_alternative,
                quantity_breaks=line.quantity_breaks,
                technical_attributes=line.technical_attributes,
                notes=line.notes,
            )

    @staticmethod
    def _apply_commercial(quotation: Any, parsed: ParsedQuotation) -> None:
        quotation.currency = parsed.currency
        quotation.payment_terms = parsed.payment_terms
        quotation.freight_amount = parsed.freight_amount
        quotation.packing_amount = parsed.packing_amount
        quotation.tooling_amount = parsed.tooling_amount
        quotation.other_charges = parsed.other_charges
        quotation.discount_amount = parsed.discount_amount
        quotation.total_amount = parsed.total_amount or Decimal(0)

    def _apply_sealed_payload(self, quotation: Any, payload: dict[str, Any]) -> None:
        quotation.currency = payload.get("currency", "")
        quotation.payment_terms = payload.get("payment_terms", "")
        quotation.freight_amount = Decimal(str(payload.get("freight_amount") or 0))
        quotation.packing_amount = Decimal(str(payload.get("packing_amount") or 0))
        quotation.tooling_amount = Decimal(str(payload.get("tooling_amount") or 0))
        quotation.other_charges = Decimal(str(payload.get("other_charges") or 0))
        quotation.discount_amount = Decimal(str(payload.get("discount_amount") or 0))
        quotation.total_amount = Decimal(str(payload.get("total_amount") or 0))

        by_line = {int(item["rfq_line_number"]): item for item in payload.get("lines", [])}
        for line in quotation.lines:
            item = by_line.get(int(line.rfq_line_number))
            if item is None:
                continue
            line.unit_price = Decimal(str(item.get("unit_price") or 0))
            line.price_per_quantity = Decimal(str(item.get("price_per_quantity") or 1))
            line.currency = item.get("currency") or quotation.currency
            line.line_total = Decimal(str(item.get("line_total") or 0))
            if item.get("quantity"):
                line.quantity = Decimal(str(item["quantity"]))
            if item.get("minimum_order_quantity"):
                line.minimum_order_quantity = Decimal(str(item["minimum_order_quantity"]))
            line.quantity_breaks = item.get("quantity_breaks") or []

    def _record_claims(
        self,
        case_id: str,
        vendor_id: str,
        quotation_id: str,
        parsed: ParsedQuotation,
        document_version_id: str,
    ) -> None:
        """A supplier's technical answers are claims, not facts.

        They are stored UNVERIFIED and attributed to SUPPLIER authority, so the
        conflict detector automatically prefers an engineering or ERP statement
        that contradicts them.
        """
        for requirement_key, answer in parsed.technical_answers.items():
            self.ctx.repos.claims.add(
                case_id=case_id,
                subject=f"{vendor_id}:{requirement_key}",
                predicate="offered_value",
                value_text=answer,
                authority="SUPPLIER",
                trust_state=TrustState.UNVERIFIED.value,
                confidence=parsed.confidence,
                document_version_id=document_version_id,
                source_location=f"quotation {quotation_id}",
                source_excerpt=answer[:1000],
                extracted_by="deterministic-quotation-parser-v1",
            )

    def _version_text(self, version: Any) -> str:
        if version.extracted_text_uri:
            try:
                return self.ctx.object_store.get(version.extracted_text_uri).decode(
                    "utf-8", errors="replace"
                )
            except Exception as exc:
                log.error("quotation_text_unreadable", detail=str(exc)[:200])
        chunks = self.ctx.repos.chunks.list_for_version(version.id)
        return "\n".join(chunk.content for chunk in chunks)
