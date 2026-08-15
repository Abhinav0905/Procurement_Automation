"""Outbound service ports.

Each protocol has a cloud adapter and a local adapter. Application services
depend only on these, which is what makes the whole pipeline runnable in CI
without a single AWS credential.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class StoredObject:
    uri: str
    key: str
    content_hash: str
    byte_size: int
    media_type: str


class ObjectStore(Protocol):
    def put(
        self,
        *,
        key: str,
        body: bytes,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> StoredObject: ...

    def get(self, uri: str) -> bytes: ...

    def exists(self, uri: str) -> bool: ...

    def presigned_url(self, uri: str, *, expires_in: int = 900) -> str: ...


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """A model call plus everything needed to audit it."""

    content: Any
    model_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    stop_reason: str = ""
    guardrail_intervened: bool = False
    raw_text: str = ""

    def metadata(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "stop_reason": self.stop_reason,
            "guardrail_intervened": self.guardrail_intervened,
        }


class LanguageModel(Protocol):
    def generate_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        purpose: str = "",
    ) -> ModelResponse: ...

    def generate_text(
        self, *, system: str, prompt: str, max_tokens: int | None = None, purpose: str = ""
    ) -> ModelResponse: ...


class EmbeddingModel(Protocol):
    @property
    def dimensions(self) -> int: ...

    @property
    def model_id(self) -> str: ...

    def embed(self, text: str) -> list[float]: ...

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True, slots=True)
class EmailAttachment:
    filename: str
    content: bytes
    media_type: str = "application/octet-stream"


@dataclass(frozen=True, slots=True)
class OutboundEmail:
    to: tuple[str, ...]
    subject: str
    body_text: str
    body_html: str = ""
    cc: tuple[str, ...] = ()
    reply_to: str = ""
    from_address: str = ""
    from_name: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    attachments: tuple[EmailAttachment, ...] = ()


@dataclass(frozen=True, slots=True)
class SendResult:
    provider: str
    provider_message_id: str
    message_id: str
    accepted: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class InboundEmail:
    message_id: str
    from_address: str
    to_addresses: tuple[str, ...]
    subject: str
    body_text: str
    body_html: str = ""
    in_reply_to: str = ""
    references: tuple[str, ...] = ()
    received_at: datetime | None = None
    attachments: tuple[EmailAttachment, ...] = ()
    raw_bytes: bytes = b""


class Mailer(Protocol):
    @property
    def provider(self) -> str: ...

    def send(self, email: OutboundEmail) -> SendResult: ...


class MailReceiver(Protocol):
    def fetch_unread(self, *, limit: int = 50) -> list[InboundEmail]: ...

    def mark_processed(self, message_id: str) -> None: ...


class EnvelopeEncryptor(Protocol):
    """Envelope encryption for sealed commercial bids."""

    def encrypt(self, plaintext: bytes, *, context: dict[str, str] | None = None) -> tuple[str, str]:
        """Returns (ciphertext_b64, key_reference)."""
        ...

    def decrypt(
        self, ciphertext_b64: str, key_reference: str, *, context: dict[str, str] | None = None
    ) -> bytes: ...


class SecretResolver(Protocol):
    def get(self, name: str) -> str: ...
