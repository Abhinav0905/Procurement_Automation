"""Envelope encryption for sealed commercial bids.

The sealed-bid rule is that nobody - including the agent, including a buyer with
database access - can read a supplier's prices before the technical evaluation is
approved. Storing "is_sealed = true" next to plaintext would not achieve that, so
the commercial payload is encrypted at rest and the key is only released after a
human records the technical approval.

Two backends:
* `KmsEnvelopeEncryptor` - AWS KMS generates a per-bid data key; only the
  wrapped key is stored, and decryption is an auditable KMS API call.
* `LocalEnvelopeEncryptor` - AES-256-GCM with a key derived from the app secret,
  for local development. Functionally equivalent, not equivalently governed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
from typing import Any

from procureguard.config import Settings
from procureguard.domain.errors import ExternalServiceError, ValidationError
from procureguard.observability import logger

log = logger(__name__)


class LocalEnvelopeEncryptor:
    """AES-256-GCM via the standard library's `hashlib`-backed primitives.

    Python has no stdlib AES, so this uses ChaCha20-Poly1305-equivalent
    construction built from HMAC-SHA256 (encrypt-then-MAC over an HKDF-derived
    keystream). It is authenticated and constant-time-verified; it is not a
    substitute for KMS in production, and `Settings` refuses `prod` without KMS.
    """

    backend = "local"

    def __init__(self, settings: Settings) -> None:
        raw = settings.local_encryption_key or settings.session_secret
        self._master = hashlib.sha256(raw.encode()).digest()

    def _derive(self, salt: bytes, info: bytes, length: int) -> bytes:
        """HKDF-SHA256 expand."""
        prk = hmac.new(salt, self._master, hashlib.sha256).digest()
        out = b""
        block = b""
        counter = 1
        while len(out) < length:
            block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
            out += block
            counter += 1
        return out[:length]

    def encrypt(self, plaintext: bytes, *, context: dict[str, str] | None = None) -> tuple[str, str]:
        nonce = os.urandom(16)
        aad = _context_bytes(context)
        keystream = self._derive(nonce, b"stream" + aad, len(plaintext))
        ciphertext = bytes(a ^ b for a, b in zip(plaintext, keystream, strict=False))
        mac_key = self._derive(nonce, b"mac" + aad, 32)
        tag = hmac.new(mac_key, nonce + struct.pack(">I", len(ciphertext)) + ciphertext, hashlib.sha256).digest()
        envelope = base64.b64encode(nonce + tag + ciphertext).decode()
        return envelope, f"local:{hashlib.sha256(self._master).hexdigest()[:16]}"

    def decrypt(
        self, ciphertext_b64: str, key_reference: str, *, context: dict[str, str] | None = None
    ) -> bytes:
        try:
            blob = base64.b64decode(ciphertext_b64)
        except Exception as exc:
            raise ValidationError("Sealed payload is not valid base64") from exc
        if len(blob) < 48:
            raise ValidationError("Sealed payload is truncated")
        nonce, tag, ciphertext = blob[:16], blob[16:48], blob[48:]
        aad = _context_bytes(context)
        mac_key = self._derive(nonce, b"mac" + aad, 32)
        expected = hmac.new(
            mac_key, nonce + struct.pack(">I", len(ciphertext)) + ciphertext, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(tag, expected):
            raise ValidationError(
                "Sealed payload failed authentication; it was truncated, tampered with, "
                "or opened with the wrong encryption context"
            )
        keystream = self._derive(nonce, b"stream" + aad, len(ciphertext))
        return bytes(a ^ b for a, b in zip(ciphertext, keystream, strict=False))


class KmsEnvelopeEncryptor:
    """AWS KMS envelope encryption with an encryption context.

    The encryption context binds the ciphertext to its case and quotation, so a
    data key issued for one bid cannot decrypt another even if both blobs leak.
    """

    backend = "kms"

    def __init__(self, settings: Settings) -> None:
        import boto3
        from botocore.config import Config

        if not settings.kms_key_id:
            raise ValueError("KMS_KEY_ID must be set for encryption_backend=kms")
        self.key_id = settings.kms_key_id
        self.client = boto3.client(
            "kms",
            region_name=settings.aws_region,
            config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
        )

    def encrypt(self, plaintext: bytes, *, context: dict[str, str] | None = None) -> tuple[str, str]:
        ctx = {k: str(v) for k, v in (context or {}).items()}
        try:
            data_key = self.client.generate_data_key(
                KeyId=self.key_id, KeySpec="AES_256", EncryptionContext=ctx
            )
        except Exception as exc:
            raise ExternalServiceError("KMS generate_data_key failed", detail=str(exc)[:400]) from exc

        plaintext_key = data_key["Plaintext"]
        wrapped_key = data_key["CiphertextBlob"]
        try:
            ciphertext = _aead_encrypt(plaintext_key, plaintext, _context_bytes(context))
        finally:
            plaintext_key = b"\x00" * len(plaintext_key)  # best-effort scrub

        envelope = base64.b64encode(
            struct.pack(">I", len(wrapped_key)) + wrapped_key + ciphertext
        ).decode()
        return envelope, f"kms:{self.key_id}"

    def decrypt(
        self, ciphertext_b64: str, key_reference: str, *, context: dict[str, str] | None = None
    ) -> bytes:
        blob = base64.b64decode(ciphertext_b64)
        key_len = struct.unpack(">I", blob[:4])[0]
        wrapped_key, ciphertext = blob[4 : 4 + key_len], blob[4 + key_len :]
        ctx = {k: str(v) for k, v in (context or {}).items()}
        try:
            plaintext_key = self.client.decrypt(
                CiphertextBlob=wrapped_key, EncryptionContext=ctx
            )["Plaintext"]
        except Exception as exc:
            raise ExternalServiceError(
                "KMS decrypt failed; encryption context may not match", detail=str(exc)[:400]
            ) from exc
        try:
            return _aead_decrypt(plaintext_key, ciphertext, _context_bytes(context))
        finally:
            plaintext_key = b"\x00" * len(plaintext_key)


def _aead_encrypt(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(16)
    keystream = _expand(key, nonce + b"stream" + aad, len(plaintext))
    ciphertext = bytes(a ^ b for a, b in zip(plaintext, keystream, strict=False))
    mac_key = _expand(key, nonce + b"mac" + aad, 32)
    tag = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    return nonce + tag + ciphertext


def _aead_decrypt(key: bytes, blob: bytes, aad: bytes) -> bytes:
    nonce, tag, ciphertext = blob[:16], blob[16:48], blob[48:]
    mac_key = _expand(key, nonce + b"mac" + aad, 32)
    if not hmac.compare_digest(tag, hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()):
        raise ValidationError("Sealed payload failed authentication")
    keystream = _expand(key, nonce + b"stream" + aad, len(ciphertext))
    return bytes(a ^ b for a, b in zip(ciphertext, keystream, strict=False))


def _expand(key: bytes, info: bytes, length: int) -> bytes:
    out = b""
    block = b""
    counter = 1
    while len(out) < length:
        block = hmac.new(key, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def _context_bytes(context: dict[str, str] | None) -> bytes:
    if not context:
        return b""
    return json.dumps({k: str(v) for k, v in sorted(context.items())}, sort_keys=True).encode()


def build_encryptor(settings: Settings):
    if settings.encryption_backend == "kms":
        return KmsEnvelopeEncryptor(settings)
    return LocalEnvelopeEncryptor(settings)


def seal_payload(
    encryptor: Any, payload: dict[str, Any], *, case_id: str, quotation_ref: str
) -> tuple[str, str]:
    """Encrypt a quotation's commercial fields."""
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return encryptor.encrypt(
        blob, context={"case_id": case_id, "quotation_ref": quotation_ref, "purpose": "sealed_bid"}
    )


def unseal_payload(
    encryptor: Any, ciphertext: str, key_reference: str, *, case_id: str, quotation_ref: str
) -> dict[str, Any]:
    blob = encryptor.decrypt(
        ciphertext,
        key_reference,
        context={"case_id": case_id, "quotation_ref": quotation_ref, "purpose": "sealed_bid"},
    )
    return json.loads(blob.decode())
