"""Outbound mail adapters.

Threading is the part that matters operationally. Every RFQ invitation carries a
per-supplier token in its Reply-To address (`rfq+<token>@domain`) *and* in a
custom header. Suppliers reply from whatever address they like, quote the mail
inline, strip headers, or forward it to a colleague - the token survives more of
that than any heuristic, and inbound matching falls back through References,
In-Reply-To, subject tag and sender domain in that order.
"""

from __future__ import annotations

import hashlib
import smtplib
import uuid
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from pathlib import Path

from procureguard.config import Settings
from procureguard.domain.errors import ExternalServiceError
from procureguard.observability import METRICS, logger
from procureguard.ports.services import EmailAttachment, OutboundEmail, SendResult

log = logger(__name__)

TOKEN_HEADER = "X-ProcureGuard-Token"
CASE_HEADER = "X-ProcureGuard-Case"
TYPE_HEADER = "X-ProcureGuard-Type"


def make_response_token(*, case_id: str, vendor_id: str, salt: str) -> str:
    """Unguessable but reproducible per (case, vendor) reply token."""
    return hashlib.blake2b(
        f"{case_id}|{vendor_id}|{salt}".encode(), digest_size=16
    ).hexdigest()


def reply_to_address(token: str, domain: str) -> str:
    return f"rfq+{token}@{domain}"


def token_from_address(address: str) -> str:
    """Recover the token from a plus-addressed recipient."""
    local = address.strip().lower().split("@", 1)[0]
    if local.startswith("rfq+"):
        return local[4:]
    if "+" in local:
        return local.split("+", 1)[1]
    return ""


def subject_tag(case_id: str) -> str:
    """Visible thread marker that survives quoting and forwarding."""
    return f"[PG-{case_id}]"


def build_message(email: OutboundEmail, settings: Settings) -> tuple[EmailMessage, str]:
    message = EmailMessage()
    from_address = email.from_address or settings.email_from_address
    from_name = email.from_name or settings.email_from_name
    message["From"] = formataddr((from_name, from_address))
    message["To"] = ", ".join(email.to)
    if email.cc:
        message["Cc"] = ", ".join(email.cc)
    if email.reply_to:
        message["Reply-To"] = email.reply_to
    message["Subject"] = email.subject
    message["Date"] = formatdate(localtime=False)
    domain = from_address.split("@", 1)[-1] or "procureguard.local"
    message_id = f"<{uuid.uuid4().hex}@{domain}>"
    message["Message-ID"] = message_id
    for key, value in (email.headers or {}).items():
        if value:
            message[key] = value

    message.set_content(email.body_text or "")
    if email.body_html:
        message.add_alternative(email.body_html, subtype="html")

    for attachment in email.attachments:
        maintype, _, subtype = (attachment.media_type or "application/octet-stream").partition("/")
        message.add_attachment(
            attachment.content,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=attachment.filename,
        )
    return message, message_id


class FilesystemMailer:
    """Writes .eml files to an outbox directory.

    This is what runs in local development and CI. It exercises the full message
    construction path - MIME, headers, attachments, threading tokens - and leaves
    an artifact a human can open, without any risk of contacting a real supplier.
    """

    provider = "filesystem"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.outbox = Path(settings.email_outbox_dir).expanduser().resolve()
        self.outbox.mkdir(parents=True, exist_ok=True)

    def send(self, email: OutboundEmail) -> SendResult:
        message, message_id = build_message(email, self.settings)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        safe_subject = "".join(c if c.isalnum() or c in "-_" else "_" for c in email.subject)[:60]
        path = self.outbox / f"{stamp}_{safe_subject}_{message_id[1:9]}.eml"
        path.write_bytes(bytes(message))
        METRICS.increment("email.sent", provider=self.provider)
        log.info(
            "email_written_to_outbox",
            path=str(path),
            to=list(email.to),
            subject=email.subject[:120],
        )
        return SendResult(
            provider=self.provider,
            provider_message_id=str(path),
            message_id=message_id,
            accepted=True,
            detail=f"Written to {path}",
        )


class SmtpMailer:
    """Plain SMTP, for on-premise relays and local MailHog/Mailpit."""

    provider = "smtp"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, email: OutboundEmail) -> SendResult:
        message, message_id = build_message(email, self.settings)
        recipients = list(email.to) + list(email.cc)
        try:
            with smtplib.SMTP(
                self.settings.smtp_host, self.settings.smtp_port, timeout=30
            ) as server:
                if self.settings.smtp_use_tls:
                    server.starttls()
                if self.settings.smtp_username:
                    server.login(self.settings.smtp_username, self.settings.smtp_password)
                server.send_message(message, to_addrs=recipients)
        except Exception as exc:
            METRICS.increment("email.failed", provider=self.provider)
            raise ExternalServiceError(
                f"SMTP send failed to {self.settings.smtp_host}", detail=str(exc)[:400]
            ) from exc
        METRICS.increment("email.sent", provider=self.provider)
        return SendResult(
            provider=self.provider,
            provider_message_id=message_id,
            message_id=message_id,
            accepted=True,
        )


class SesMailer:
    """Amazon SES v2, using SendRawEmail semantics so attachments and custom
    threading headers survive intact."""

    provider = "ses"

    def __init__(self, settings: Settings) -> None:
        import boto3
        from botocore.config import Config

        self.settings = settings
        self.client = boto3.client(
            "sesv2",
            region_name=settings.aws_region,
            config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
        )

    def send(self, email: OutboundEmail) -> SendResult:
        message, message_id = build_message(email, self.settings)
        try:
            response = self.client.send_email(
                FromEmailAddress=formataddr(
                    (
                        email.from_name or self.settings.email_from_name,
                        email.from_address or self.settings.email_from_address,
                    )
                ),
                Destination={
                    "ToAddresses": list(email.to),
                    "CcAddresses": list(email.cc),
                },
                Content={"Raw": {"Data": bytes(message)}},
                ReplyToAddresses=[email.reply_to] if email.reply_to else [],
            )
        except Exception as exc:
            METRICS.increment("email.failed", provider=self.provider)
            raise ExternalServiceError("SES send failed", detail=str(exc)[:400]) from exc
        METRICS.increment("email.sent", provider=self.provider)
        return SendResult(
            provider=self.provider,
            provider_message_id=str(response.get("MessageId", "")),
            message_id=message_id,
            accepted=True,
        )


def build_mailer(settings: Settings):
    match settings.email_backend:
        case "ses":
            return SesMailer(settings)
        case "smtp":
            return SmtpMailer(settings)
        case _:
            return FilesystemMailer(settings)


__all__ = [
    "CASE_HEADER",
    "EmailAttachment",
    "FilesystemMailer",
    "SesMailer",
    "SmtpMailer",
    "TOKEN_HEADER",
    "TYPE_HEADER",
    "build_mailer",
    "build_message",
    "make_response_token",
    "reply_to_address",
    "subject_tag",
    "token_from_address",
]
