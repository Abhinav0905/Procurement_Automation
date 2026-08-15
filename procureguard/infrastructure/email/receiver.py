"""Inbound mail adapters and MIME parsing.

Everything a supplier sends is hostile input until proven otherwise: the raw
bytes are stored first, the parsed body goes through the document firewall, and
attachments are size- and type-checked before anything reads them.
"""

from __future__ import annotations

import email
import imaplib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path

from procureguard.config import Settings
from procureguard.domain.errors import ExternalServiceError
from procureguard.observability import logger
from procureguard.ports.services import EmailAttachment, InboundEmail

log = logger(__name__)

# Attachment types a quotation legitimately arrives as.
ALLOWED_ATTACHMENT_TYPES = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/msword",
        "text/plain",
        "text/csv",
        "image/png",
        "image/jpeg",
        "application/octet-stream",
    }
)

# Executable and script types are never opened, only quarantined.
BLOCKED_EXTENSIONS = frozenset(
    {
        ".exe", ".dll", ".scr", ".bat", ".cmd", ".com", ".js", ".vbs", ".ps1",
        ".jar", ".msi", ".app", ".sh", ".lnk", ".iso", ".hta",
    }
)


def parse_mime(raw: bytes, *, max_attachment_bytes: int = 32 * 1024 * 1024) -> InboundEmail:
    """Parse an RFC 5322 message into the shape the mailroom consumes."""
    message: EmailMessage = email.message_from_bytes(raw, policy=policy.default)  # type: ignore[assignment]

    body_text, body_html = _extract_bodies(message)
    attachments = tuple(_extract_attachments(message, max_attachment_bytes))

    received_at: datetime | None = None
    if message.get("Date"):
        try:
            received_at = parsedate_to_datetime(message["Date"])
        except (TypeError, ValueError):
            received_at = None
    if received_at is None:
        received_at = datetime.now(UTC)
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=UTC)

    references = tuple(
        ref.strip() for ref in (message.get("References", "") or "").split() if ref.strip()
    )
    return InboundEmail(
        message_id=(message.get("Message-ID", "") or "").strip(),
        from_address=_address_of(message.get("From", "")),
        to_addresses=tuple(
            _address_of(part)
            for part in (message.get_all("To", []) + message.get_all("Delivered-To", []))
        ),
        subject=str(message.get("Subject", "") or ""),
        body_text=body_text,
        body_html=body_html,
        in_reply_to=(message.get("In-Reply-To", "") or "").strip(),
        references=references,
        received_at=received_at,
        attachments=attachments,
        raw_bytes=raw,
    )


def _extract_bodies(message: EmailMessage) -> tuple[str, str]:
    text, html = "", ""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_filename():
                continue
            content_type = part.get_content_type()
            try:
                payload = part.get_content()
            except Exception:
                raw = part.get_payload(decode=True) or b""
                payload = raw.decode("utf-8", errors="replace")
            if content_type == "text/plain" and not text:
                text = str(payload)
            elif content_type == "text/html" and not html:
                html = str(payload)
    else:
        try:
            content = str(message.get_content())
        except Exception:
            content = (message.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
        if message.get_content_type() == "text/html":
            html = content
        else:
            text = content
    if not text and html:
        text = strip_html(html)
    return text, html


def _extract_attachments(
    message: EmailMessage, max_bytes: int
) -> Iterable[EmailAttachment]:
    for part in message.walk():
        filename = part.get_filename()
        if not filename:
            continue
        suffix = Path(filename).suffix.lower()
        if suffix in BLOCKED_EXTENSIONS:
            log.warning("attachment_blocked_by_extension", filename=filename)
            continue
        content = part.get_payload(decode=True) or b""
        if not content:
            continue
        if len(content) > max_bytes:
            log.warning("attachment_too_large", filename=filename, size=len(content))
            continue
        yield EmailAttachment(
            filename=filename[:500],
            content=content,
            media_type=part.get_content_type() or "application/octet-stream",
        )


def strip_html(html: str) -> str:
    """Crude but dependency-free HTML-to-text for quotation bodies."""
    import re

    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|tr|li|h[1-6])>", "\n", text, flags=re.I)
    text = re.sub(r"</t[dh]>", "\t", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    replacements = {
        "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
        "&quot;": '"', "&#39;": "'", "&euro;": "EUR", "&pound;": "GBP",
    }
    for entity, char in replacements.items():
        text = text.replace(entity, char)
    text = re.sub(r"[ \t]+", " ", text)
    return "\n".join(line.strip() for line in text.split("\n") if line.strip())


def _address_of(header_value: str) -> str:
    from email.utils import parseaddr

    return parseaddr(str(header_value or ""))[1].strip().lower()


class FilesystemMailReceiver:
    """Reads .eml files from an inbox directory.

    The local counterpart to `FilesystemMailer`: drop a supplier reply into
    `var/inbox/` and the mailroom picks it up exactly as it would from IMAP.
    """

    provider = "filesystem"

    def __init__(self, settings: Settings, *, inbox_dir: str = "") -> None:
        root = Path(inbox_dir or settings.email_outbox_dir).expanduser().resolve().parent
        self.inbox = Path(inbox_dir) if inbox_dir else root / "inbox"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.processed = self.inbox / ".processed"
        self.processed.mkdir(exist_ok=True)

    def fetch_unread(self, *, limit: int = 50) -> list[InboundEmail]:
        results: list[InboundEmail] = []
        for path in sorted(self.inbox.glob("*.eml"))[:limit]:
            try:
                results.append(parse_mime(path.read_bytes()))
            except Exception as exc:
                log.error("inbound_parse_failed", path=str(path), detail=str(exc)[:300])
        return results

    def mark_processed(self, message_id: str) -> None:
        digest = "".join(c for c in message_id if c.isalnum())[:40] or "unknown"
        for path in self.inbox.glob("*.eml"):
            try:
                parsed = parse_mime(path.read_bytes())
            except Exception:
                continue
            if parsed.message_id == message_id:
                path.rename(self.processed / path.name)
                return
        (self.processed / f"{digest}.marker").write_text(message_id, encoding="utf-8")


class ImapMailReceiver:
    """IMAP polling for shared procurement mailboxes (Exchange, Gmail, Zimbra)."""

    provider = "imap"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if not settings.imap_host:
            raise ValueError("IMAP_HOST must be configured for the IMAP receiver")

    def _connect(self) -> imaplib.IMAP4_SSL:
        try:
            client = imaplib.IMAP4_SSL(self.settings.imap_host, self.settings.imap_port)
            client.login(self.settings.imap_username, self.settings.imap_password)
            client.select(self.settings.imap_mailbox)
            return client
        except Exception as exc:
            raise ExternalServiceError(
                f"IMAP connection to {self.settings.imap_host} failed", detail=str(exc)[:300]
            ) from exc

    def fetch_unread(self, *, limit: int = 50) -> list[InboundEmail]:
        client = self._connect()
        try:
            status, data = client.search(None, "UNSEEN")
            if status != "OK":
                return []
            uids = (data[0] or b"").split()[:limit]
            out: list[InboundEmail] = []
            for uid in uids:
                status, payload = client.fetch(uid, "(BODY.PEEK[])")
                if status != "OK" or not payload or not isinstance(payload[0], tuple):
                    continue
                try:
                    out.append(parse_mime(payload[0][1]))
                except Exception as exc:
                    log.error("imap_parse_failed", uid=uid.decode(), detail=str(exc)[:300])
            return out
        finally:
            try:
                client.logout()
            except Exception:
                pass

    def mark_processed(self, message_id: str) -> None:
        client = self._connect()
        try:
            status, data = client.search(None, f'(HEADER Message-ID "{message_id}")')
            if status == "OK":
                for uid in (data[0] or b"").split():
                    client.store(uid, "+FLAGS", "\\Seen")
        finally:
            try:
                client.logout()
            except Exception:
                pass


class SesS3MailReceiver:
    """SES inbound rule -> S3 -> here.

    Production inbound path: SES writes the raw MIME to a bucket and notifies
    via SNS/EventBridge; this reads the object by key. Nothing is deleted, so a
    disputed quotation can always be re-parsed from the original bytes.
    """

    provider = "ses-s3"

    def __init__(self, settings: Settings) -> None:
        from procureguard.infrastructure.storage.object_store import S3ObjectStore

        self.settings = settings
        self.store = S3ObjectStore(settings, bucket=settings.s3_inbound_email_bucket)

    def fetch_from_key(self, key: str) -> InboundEmail:
        raw = self.store.get(f"s3://{self.settings.s3_inbound_email_bucket}/{key}")
        return parse_mime(raw)

    def fetch_from_notification(self, payload: dict) -> list[InboundEmail]:
        """Parse an SNS/EventBridge S3 notification into inbound messages."""
        records = payload.get("Records") or []
        if not records and "Message" in payload:
            try:
                records = json.loads(payload["Message"]).get("Records", [])
            except (TypeError, ValueError):
                records = []
        out: list[InboundEmail] = []
        for record in records:
            key = record.get("s3", {}).get("object", {}).get("key")
            if key:
                out.append(self.fetch_from_key(key))
        return out

    def fetch_unread(self, *, limit: int = 50) -> list[InboundEmail]:
        # SES inbound is push-driven; polling is not the intended entry point.
        return []

    def mark_processed(self, message_id: str) -> None:
        return None


def build_mail_receiver(settings: Settings, *, inbox_dir: str = ""):
    if settings.email_backend == "ses":
        return SesS3MailReceiver(settings)
    if settings.imap_host:
        return ImapMailReceiver(settings)
    return FilesystemMailReceiver(settings, inbox_dir=inbox_dir)
