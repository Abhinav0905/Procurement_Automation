"""Stage 8 - real email integration.

Outbound and inbound supplier correspondence, with three properties that matter
in production:

* **Nothing is sent by accident.** `ALLOW_AUTOMATED_EMAIL_SEND=false` (the
  default) stores a fully-rendered message in `PENDING_APPROVAL` instead of
  transmitting it. The agent drafts; a human releases.
* **Nothing is sent twice.** Every send is claimed against an idempotency key
  before transmission, so a Temporal activity retry after a timeout cannot
  double-mail a supplier.
* **Replies find their case.** Inbound matching walks five signals in order of
  reliability, because suppliers reply from personal addresses, strip headers,
  and forward threads to colleagues.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from procureguard.domain.enums import (
    CommunicationDirection,
    CommunicationStatus,
    CommunicationType,
    RfqInvitationStatus,
)
from procureguard.domain.errors import PolicyViolationError
from procureguard.infrastructure.email.mailer import (
    CASE_HEADER,
    TOKEN_HEADER,
    TYPE_HEADER,
    subject_tag,
    token_from_address,
)
from procureguard.infrastructure.factory import ServiceContext
from procureguard.infrastructure.storage.object_store import content_key
from procureguard.observability import METRICS, logger
from procureguard.ports.services import EmailAttachment, InboundEmail, OutboundEmail

log = logger(__name__)


@dataclass(slots=True)
class SendOutcome:
    communication_id: str
    status: str
    transmitted: bool
    provider_message_id: str = ""
    external_message_id: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "communication_id": self.communication_id,
            "status": self.status,
            "transmitted": self.transmitted,
            "provider_message_id": self.provider_message_id,
            "external_message_id": self.external_message_id,
            "reason": self.reason,
        }


@dataclass(slots=True)
class InboundOutcome:
    communication_id: str
    case_id: str
    vendor_id: str
    matched_by: str
    classification: str
    quarantined: bool
    attachment_version_ids: list[str] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    unmatched: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "communication_id": self.communication_id,
            "case_id": self.case_id,
            "vendor_id": self.vendor_id,
            "matched_by": self.matched_by,
            "classification": self.classification,
            "quarantined": self.quarantined,
            "attachment_version_ids": self.attachment_version_ids,
            "findings": self.findings,
            "unmatched": self.unmatched,
        }


class MailroomService:
    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx

    # ═════════════════════════════════════════════════════════════ outbound
    def send(
        self,
        *,
        case_id: str,
        vendor_id: str,
        communication_type: CommunicationType,
        to: list[str],
        subject: str,
        body_text: str,
        idempotency_key: str,
        rfq_id: str = "",
        invitation_id: str = "",
        reply_to: str = "",
        thread_token: str = "",
        cc: list[str] | None = None,
        attachments: list[EmailAttachment] | None = None,
        in_reply_to: str = "",
        is_external: bool = True,
    ) -> SendOutcome:
        """Draft, gate, record and (if permitted) transmit one message."""
        existing = self.ctx.repos.communications.find_by_idempotency_key(idempotency_key)
        if existing is not None:
            log.info(
                "email_send_deduplicated",
                idempotency_key=idempotency_key,
                communication_id=existing.id,
            )
            return SendOutcome(
                communication_id=existing.id,
                status=existing.status,
                transmitted=existing.status in ("SENT", "DELIVERED"),
                provider_message_id=existing.provider_message_id,
                external_message_id=existing.external_message_id,
                reason="Already processed under this idempotency key",
            )

        tagged_subject = subject if subject_tag(case_id) in subject else f"{subject_tag(case_id)} {subject}"
        body_hash = hashlib.sha256(body_text.encode()).hexdigest()

        attachment_refs: list[dict[str, str]] = []
        for attachment in attachments or []:
            stored = self.ctx.object_store.put(
                key=content_key(
                    prefix="outbound-attachments",
                    content=attachment.content,
                    filename=attachment.filename,
                ),
                body=attachment.content,
                content_type=attachment.media_type,
                metadata={"case_id": case_id},
            )
            attachment_refs.append(
                {"filename": attachment.filename, "uri": stored.uri, "hash": stored.content_hash}
            )

        communication = self.ctx.repos.communications.create(
            case_id=case_id,
            rfq_id=rfq_id,
            invitation_id=invitation_id,
            vendor_id=vendor_id,
            communication_type=communication_type.value,
            direction=CommunicationDirection.OUTBOUND.value,
            status=CommunicationStatus.DRAFT.value,
            from_address=self.ctx.settings.email_from_address,
            to_addresses=list(to),
            cc_addresses=list(cc or []),
            reply_to=reply_to,
            subject=tagged_subject,
            body_text=body_text,
            body_hash=body_hash,
            thread_token=thread_token,
            in_reply_to=in_reply_to,
            idempotency_key=idempotency_key,
            attachment_refs=attachment_refs,
            requires_release_by="" if not is_external else "PROCUREMENT",
        )

        decision = self.ctx.policy.may_transmit_email(is_external=is_external)
        if not decision.allowed:
            communication.status = CommunicationStatus.PENDING_APPROVAL.value
            communication.error_detail = decision.reason
            self.ctx.session.flush()
            self.ctx.audit(
                entity_type="COMMUNICATION",
                entity_id=communication.id,
                case_id=case_id,
                action="EMAIL_HELD_FOR_RELEASE",
                after_state={"to": to, "subject": tagged_subject, "reason": decision.reason},
            )
            METRICS.increment("email.suppressed", type=communication_type.value)
            log.info(
                "email_held_for_human_release",
                case_id=case_id,
                vendor_id=vendor_id,
                type=communication_type.value,
            )
            return SendOutcome(
                communication_id=communication.id,
                status=communication.status,
                transmitted=False,
                reason=decision.reason,
            )

        # Claim the key *before* the network call: a retry after a timeout must
        # not be able to send a second copy.
        claimed, _prior = self.ctx.repos.idempotency.claim(
            f"email:{idempotency_key}", scope="email_send"
        )
        if not claimed:
            communication.status = CommunicationStatus.SUPPRESSED.value
            self.ctx.session.flush()
            return SendOutcome(
                communication_id=communication.id,
                status=communication.status,
                transmitted=False,
                reason="A concurrent attempt already claimed this send",
            )

        headers = {
            TOKEN_HEADER: thread_token,
            CASE_HEADER: case_id,
            TYPE_HEADER: communication_type.value,
        }
        if in_reply_to:
            headers["In-Reply-To"] = in_reply_to
            headers["References"] = in_reply_to

        email = OutboundEmail(
            to=tuple(to),
            cc=tuple(cc or ()),
            subject=tagged_subject,
            body_text=body_text,
            reply_to=reply_to,
            from_address=self.ctx.settings.email_from_address,
            from_name=self.ctx.settings.email_from_name,
            headers=headers,
            attachments=tuple(attachments or ()),
        )
        try:
            sent = self.ctx.mailer.send(email)
        except Exception as exc:
            self.ctx.repos.idempotency.release(f"email:{idempotency_key}")
            self.ctx.repos.communications.mark_failed(communication.id, error=str(exc)[:2000])
            log.error("email_send_failed", case_id=case_id, vendor_id=vendor_id, detail=str(exc)[:300])
            raise

        communication.external_message_id = sent.message_id
        self.ctx.repos.communications.mark_sent(
            communication.id,
            provider=sent.provider,
            provider_message_id=sent.provider_message_id,
        )
        self.ctx.repos.idempotency.complete(
            f"email:{idempotency_key}",
            result_ref=communication.id,
            payload={"message_id": sent.message_id},
        )
        self.ctx.audit(
            entity_type="COMMUNICATION",
            entity_id=communication.id,
            case_id=case_id,
            action="EMAIL_SENT",
            after_state={"to": to, "subject": tagged_subject, "provider": sent.provider},
        )
        log.info(
            "email_sent",
            case_id=case_id,
            vendor_id=vendor_id,
            type=communication_type.value,
            provider=sent.provider,
        )
        return SendOutcome(
            communication_id=communication.id,
            status=CommunicationStatus.SENT.value,
            transmitted=True,
            provider_message_id=sent.provider_message_id,
            external_message_id=sent.message_id,
        )

    def release_held(self, communication_id: str, *, actor_id: str) -> SendOutcome:
        """Human release of a message that policy withheld."""
        communication = self.ctx.repos.communications.get(communication_id)
        if communication is None:
            raise PolicyViolationError(f"Communication {communication_id} not found")
        if communication.status not in (
            CommunicationStatus.PENDING_APPROVAL.value,
            CommunicationStatus.SUPPRESSED.value,
        ):
            return SendOutcome(
                communication_id=communication.id,
                status=communication.status,
                transmitted=communication.status == CommunicationStatus.SENT.value,
                reason="Message is not awaiting release",
            )

        email = OutboundEmail(
            to=tuple(communication.to_addresses or ()),
            cc=tuple(communication.cc_addresses or ()),
            subject=communication.subject,
            body_text=communication.body_text,
            reply_to=communication.reply_to,
            from_address=self.ctx.settings.email_from_address,
            from_name=self.ctx.settings.email_from_name,
            headers={
                TOKEN_HEADER: communication.thread_token,
                CASE_HEADER: communication.case_id,
                TYPE_HEADER: communication.communication_type,
            },
            attachments=tuple(self._rehydrate_attachments(communication.attachment_refs or [])),
        )
        sent = self.ctx.mailer.send(email)
        communication.external_message_id = sent.message_id
        communication.released_by = actor_id
        self.ctx.repos.communications.mark_sent(
            communication.id, provider=sent.provider, provider_message_id=sent.provider_message_id
        )
        self._advance_invitation_on_send(communication)
        self.ctx.audit(
            entity_type="COMMUNICATION",
            entity_id=communication.id,
            case_id=communication.case_id,
            action="EMAIL_RELEASED_BY_HUMAN",
            actor_id=actor_id,
            after_state={"to": communication.to_addresses, "provider": sent.provider},
        )
        return SendOutcome(
            communication_id=communication.id,
            status=CommunicationStatus.SENT.value,
            transmitted=True,
            provider_message_id=sent.provider_message_id,
            external_message_id=sent.message_id,
        )

    def _rehydrate_attachments(self, refs: list[dict[str, str]]) -> list[EmailAttachment]:
        out: list[EmailAttachment] = []
        for ref in refs:
            try:
                out.append(
                    EmailAttachment(
                        filename=ref.get("filename", "attachment"),
                        content=self.ctx.object_store.get(ref["uri"]),
                    )
                )
            except Exception as exc:
                log.error("attachment_rehydrate_failed", uri=ref.get("uri"), detail=str(exc)[:200])
        return out

    def _advance_invitation_on_send(self, communication: Any) -> None:
        if not communication.invitation_id:
            return
        invitation = self.ctx.repos.rfqs.get_invitation(communication.invitation_id)
        if invitation is None:
            return
        now = datetime.now(UTC)
        invitation.last_contact_at = now
        invitation.thread_message_id = (
            communication.external_message_id or invitation.thread_message_id
        )
        if communication.communication_type == CommunicationType.RFQ_INVITATION.value:
            invitation.status = RfqInvitationStatus.SENT.value
            invitation.sent_at = invitation.sent_at or now
        elif communication.communication_type == CommunicationType.RFQ_REMINDER.value:
            invitation.reminders_sent += 1
        self.ctx.session.flush()

    # ═════════════════════════════════════════════════════════════ inbound
    def receive(self, message: InboundEmail) -> InboundOutcome:
        """Ingest one supplier reply: match, scan, store, classify."""
        duplicate = self.ctx.repos.communications.find_by_external_id(message.message_id)
        if duplicate is not None:
            log.info("inbound_duplicate_ignored", message_id=message.message_id)
            return InboundOutcome(
                communication_id=duplicate.id,
                case_id=duplicate.case_id,
                vendor_id=duplicate.vendor_id,
                matched_by="duplicate",
                classification=duplicate.communication_type,
                quarantined=False,
            )

        invitation, matched_by = self._match(message)
        case_id = invitation.case_id if invitation else ""
        vendor_id = invitation.vendor_id if invitation else ""

        vendor = self.ctx.repos.vendors.get(vendor_id) if vendor_id else None
        if vendor is None:
            vendor = self.ctx.repos.vendors.find_by_email_domain(message.from_address)
            if vendor is not None and not vendor_id:
                vendor_id = vendor.vendor_id
                matched_by = matched_by or "sender_domain"

        known_domain = ""
        if vendor and vendor.email and "@" in vendor.email:
            known_domain = vendor.email.split("@", 1)[1]

        scan = self.ctx.firewall.scan_email(
            subject=message.subject,
            body=message.body_text,
            from_address=message.from_address,
            known_vendor_domain=known_domain,
        )

        raw_stored = self.ctx.object_store.put(
            key=content_key(
                prefix="inbound-email",
                content=message.raw_bytes or message.body_text.encode(),
                extension=".eml",
            ),
            body=message.raw_bytes or message.body_text.encode(),
            content_type="message/rfc822",
            metadata={"case_id": case_id, "from": message.from_address[:200]},
        )

        classification = self._classify(message, invitation)
        communication = self.ctx.repos.communications.create(
            case_id=case_id,
            rfq_id=invitation.rfq_id if invitation else "",
            invitation_id=invitation.id if invitation else "",
            vendor_id=vendor_id,
            communication_type=classification.value,
            direction=CommunicationDirection.INBOUND.value,
            status=CommunicationStatus.RECEIVED.value,
            from_address=message.from_address,
            to_addresses=list(message.to_addresses),
            subject=message.subject,
            body_text=message.body_text[:200_000],
            body_html=message.body_html[:200_000],
            body_hash=hashlib.sha256(message.body_text.encode()).hexdigest(),
            external_message_id=message.message_id,
            in_reply_to=message.in_reply_to,
            thread_token=invitation.response_token if invitation else "",
            idempotency_key=f"inbound:{message.message_id or raw_stored.content_hash}",
            received_at=message.received_at or datetime.now(UTC),
            storage_uri=raw_stored.uri,
        )

        if scan.findings:
            self.ctx.repos.findings.record_many(
                scan.findings, case_id=case_id, communication_id=communication.id
            )
        quarantined = scan.is_quarantined
        if quarantined:
            communication.status = CommunicationStatus.RECEIVED.value
            communication.error_detail = (
                "Quarantined by the document firewall: " + ", ".join(sorted(scan.finding_types))
            )
            METRICS.increment("firewall.email_quarantined")

        # Store attachments as document versions regardless of verdict, so a
        # human can inspect exactly what arrived.
        attachment_version_ids: list[str] = []
        if case_id:
            from procureguard.application.document_ingestion import DocumentIngestionService
            from procureguard.domain.enums import DocumentAuthority, DocumentType

            ingestion = DocumentIngestionService(self.ctx)
            for attachment in message.attachments:
                result = ingestion.ingest(
                    content=attachment.content,
                    filename=attachment.filename,
                    case_id=case_id,
                    document_type=DocumentType.QUOTATION,
                    authority=DocumentAuthority.SUPPLIER,
                    media_type=attachment.media_type,
                    vendor_id=vendor_id,
                    received_from=message.from_address,
                )
                attachment_version_ids.append(result.document_version_id)
            communication.attachment_refs = [
                {"filename": a.filename, "document_version_id": v}
                for a, v in zip(message.attachments, attachment_version_ids, strict=True)
            ]

        if invitation is not None and classification in (
            CommunicationType.QUOTATION_RECEIPT,
            CommunicationType.NEGOTIATION_RESPONSE,
        ):
            invitation.status = RfqInvitationStatus.QUOTED.value
            invitation.responded_at = datetime.now(UTC)
        elif invitation is not None and classification == CommunicationType.CLARIFICATION_RESPONSE:
            invitation.status = RfqInvitationStatus.ACKNOWLEDGED.value
        elif invitation is not None and _is_decline(message):
            invitation.status = RfqInvitationStatus.DECLINED.value
            invitation.declined_reason = message.body_text[:2000]
            invitation.responded_at = datetime.now(UTC)
        self.ctx.session.flush()

        self.ctx.audit(
            entity_type="COMMUNICATION",
            entity_id=communication.id,
            case_id=case_id,
            action="EMAIL_RECEIVED",
            after_state={
                "from": message.from_address,
                "subject": message.subject[:200],
                "matched_by": matched_by,
                "classification": classification.value,
                "quarantined": quarantined,
                "attachments": len(message.attachments),
            },
        )
        log.info(
            "inbound_email_processed",
            case_id=case_id or "(unmatched)",
            vendor_id=vendor_id or "(unknown)",
            matched_by=matched_by or "none",
            classification=classification.value,
            quarantined=quarantined,
            attachments=len(message.attachments),
        )
        return InboundOutcome(
            communication_id=communication.id,
            case_id=case_id,
            vendor_id=vendor_id,
            matched_by=matched_by or "unmatched",
            classification=classification.value,
            quarantined=quarantined,
            attachment_version_ids=attachment_version_ids,
            findings=[f.to_dict() for f in scan.findings],
            unmatched=invitation is None,
        )

    def _match(self, message: InboundEmail) -> tuple[Any, str]:
        """Route an inbound message to its RFQ invitation.

        Ordered by reliability. The plus-address token is cryptographic and
        survives quoting; the sender domain is a last resort and can be forged,
        which is why a domain-only match is also flagged by the firewall.
        """
        # 1. Plus-addressed reply token.
        for address in message.to_addresses:
            token = token_from_address(address)
            if token:
                invitation = self.ctx.repos.rfqs.find_invitation_by_token(token)
                if invitation is not None:
                    return invitation, "reply_token"

        # 2. In-Reply-To / References against a message we sent.
        for candidate in filter(None, (message.in_reply_to, *message.references)):
            prior = self.ctx.repos.communications.find_by_external_id(candidate)
            if prior is not None and prior.invitation_id:
                invitation = self.ctx.repos.rfqs.get_invitation(prior.invitation_id)
                if invitation is not None:
                    return invitation, "in_reply_to"

        # 3. Subject tag [PG-<case_id>] plus the sender's vendor identity.
        import re

        tag = re.search(r"\[PG-([A-Za-z0-9\-_]+)\]", message.subject or "")
        if tag:
            case_id = f"PG-{tag.group(1)}" if not tag.group(1).startswith("PG-") else tag.group(1)
            vendor = self.ctx.repos.vendors.find_by_email_domain(message.from_address)
            if vendor is not None:
                invitation = self.ctx.repos.rfqs.find_invitation(case_id, vendor.vendor_id)
                if invitation is not None:
                    return invitation, "subject_tag"

        # 4. RFQ number quoted anywhere in the body.
        rfq_match = re.search(r"\bRFQ-\d{4}-\d{6}\b", f"{message.subject}\n{message.body_text}")
        if rfq_match:
            rfq = self.ctx.repos.rfqs.get_by_number(rfq_match.group(0))
            if rfq is not None:
                vendor = self.ctx.repos.vendors.find_by_email_domain(message.from_address)
                if vendor is not None:
                    invitation = self.ctx.repos.rfqs.find_invitation(rfq.case_id, vendor.vendor_id)
                    if invitation is not None:
                        return invitation, "rfq_number"
        return None, ""

    @staticmethod
    def _classify(message: InboundEmail, invitation: Any) -> CommunicationType:
        text = f"{message.subject}\n{message.body_text}".lower()
        if _is_decline(message):
            return CommunicationType.CLARIFICATION_RESPONSE
        has_priced_attachment = bool(message.attachments)
        looks_priced = any(
            token in text
            for token in ("unit price", "total price", "quotation", "quote no", "our offer", "pricing")
        )
        if has_priced_attachment or looks_priced:
            if invitation is not None and invitation.status == RfqInvitationStatus.QUOTED.value:
                return CommunicationType.NEGOTIATION_RESPONSE
            return CommunicationType.QUOTATION_RECEIPT
        if "?" in text or any(
            token in text for token in ("clarif", "question", "please confirm", "could you")
        ):
            return CommunicationType.CLARIFICATION_RESPONSE
        return CommunicationType.UNCLASSIFIED

    def poll(self, *, limit: int = 50) -> list[InboundOutcome]:
        """Fetch and process everything waiting in the inbound mailbox."""
        from procureguard.infrastructure.factory import get_mail_receiver

        receiver = get_mail_receiver()
        outcomes: list[InboundOutcome] = []
        for message in receiver.fetch_unread(limit=limit):
            try:
                outcome = self.receive(message)
                outcomes.append(outcome)
                receiver.mark_processed(message.message_id)
            except Exception as exc:
                log.error(
                    "inbound_processing_failed",
                    message_id=message.message_id,
                    detail=str(exc)[:300],
                )
        return outcomes


def _is_decline(message: InboundEmail) -> bool:
    text = f"{message.subject}\n{message.body_text}".lower()
    return any(
        phrase in text
        for phrase in (
            "no bid", "not bidding", "decline to quote", "declining to quote",
            "unable to quote", "cannot quote", "we will not be quoting",
            "regret that we are unable", "not in a position to offer",
        )
    )
