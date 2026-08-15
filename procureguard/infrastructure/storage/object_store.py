"""Object storage adapters.

Raw artifacts are written before interpretation and never mutated: the bytes an
evaluation saw must still be retrievable when someone challenges the award. Keys
are content-addressed, so storing the same document twice is free and idempotent.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from procureguard.config import Settings
from procureguard.domain.errors import ExternalServiceError, NotFoundError, ValidationError
from procureguard.observability import logger
from procureguard.ports.services import StoredObject

log = logger(__name__)


def content_key(
    *, prefix: str, content: bytes, filename: str = "", extension: str = ""
) -> str:
    """Content-addressed key: <prefix>/<aa>/<hash><ext>.

    The two-character fan-out keeps S3 prefixes from hot-spotting and keeps
    local directories browsable.
    """
    digest = hashlib.sha256(content).hexdigest()
    ext = extension or (Path(filename).suffix if filename else "")
    return f"{prefix.strip('/')}/{digest[:2]}/{digest}{ext}"


def guess_media_type(filename: str, fallback: str = "application/octet-stream") -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or fallback


class LocalObjectStore:
    """Filesystem-backed store for local development, CI and demos."""

    scheme = "file"

    def __init__(self, settings: Settings) -> None:
        self.root = Path(settings.object_store_local_root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = settings.document_max_bytes

    def _path_for(self, key: str) -> Path:
        # Refuse traversal even though keys are generated internally.
        candidate = (self.root / key.lstrip("/")).resolve()
        if not str(candidate).startswith(str(self.root)):
            raise ValidationError(f"Object key escapes the store root: {key!r}")
        return candidate

    def put(
        self,
        *,
        key: str,
        body: bytes,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        if len(body) > self.max_bytes:
            raise ValidationError(
                f"Object exceeds the {self.max_bytes} byte limit", size=len(body), key=key
            )
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            # Write-then-rename so a reader never sees a partial object.
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(body)
            os.replace(tmp, path)
        if metadata:
            path.with_suffix(path.suffix + ".meta").write_text(
                "\n".join(f"{k}={v}" for k, v in metadata.items()), encoding="utf-8"
            )
        return StoredObject(
            uri=f"file://{path}",
            key=key,
            content_hash=hashlib.sha256(body).hexdigest(),
            byte_size=len(body),
            media_type=content_type,
        )

    def get(self, uri: str) -> bytes:
        path = Path(urlparse(uri).path) if uri.startswith("file://") else self._path_for(uri)
        if not path.exists():
            raise NotFoundError(f"Object not found: {uri}", uri=uri)
        return path.read_bytes()

    def exists(self, uri: str) -> bool:
        try:
            path = Path(urlparse(uri).path) if uri.startswith("file://") else self._path_for(uri)
            return path.exists()
        except ValidationError:
            return False

    def presigned_url(self, uri: str, *, expires_in: int = 900) -> str:
        # No signing locally; the API serves these through an authorised route.
        return uri

    def delete(self, uri: str) -> None:
        path = Path(urlparse(uri).path) if uri.startswith("file://") else self._path_for(uri)
        path.unlink(missing_ok=True)

    def clear(self) -> None:  # test helper
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)


class S3ObjectStore:
    """S3 with SSE-KMS, versioning-friendly content-addressed keys."""

    scheme = "s3"

    def __init__(self, settings: Settings, *, bucket: str = "") -> None:
        import boto3
        from botocore.config import Config

        self.settings = settings
        self.bucket = bucket or settings.s3_bucket
        self.max_bytes = settings.document_max_bytes
        self.client = boto3.client(
            "s3",
            region_name=settings.aws_region,
            config=Config(
                retries={"max_attempts": 5, "mode": "adaptive"},
                signature_version="s3v4",
            ),
        )

    def _encryption_args(self) -> dict[str, Any]:
        if self.settings.s3_kms_key_id:
            return {
                "ServerSideEncryption": "aws:kms",
                "SSEKMSKeyId": self.settings.s3_kms_key_id,
            }
        return {"ServerSideEncryption": "AES256"}

    def put(
        self,
        *,
        key: str,
        body: bytes,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        if len(body) > self.max_bytes:
            raise ValidationError(
                f"Object exceeds the {self.max_bytes} byte limit", size=len(body), key=key
            )
        digest = hashlib.sha256(body).hexdigest()
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
                Metadata={**(metadata or {}), "sha256": digest},
                **self._encryption_args(),
            )
        except Exception as exc:
            raise ExternalServiceError(f"S3 put failed for {key}", key=key, detail=str(exc)) from exc
        return StoredObject(
            uri=f"s3://{self.bucket}/{key}",
            key=key,
            content_hash=digest,
            byte_size=len(body),
            media_type=content_type,
        )

    def _split(self, uri: str) -> tuple[str, str]:
        if uri.startswith("s3://"):
            parsed = urlparse(uri)
            return parsed.netloc, parsed.path.lstrip("/")
        return self.bucket, uri.lstrip("/")

    def get(self, uri: str) -> bytes:
        bucket, key = self._split(uri)
        try:
            return self.client.get_object(Bucket=bucket, Key=key)["Body"].read()
        except self.client.exceptions.NoSuchKey as exc:
            raise NotFoundError(f"Object not found: {uri}", uri=uri) from exc
        except Exception as exc:
            raise ExternalServiceError(f"S3 get failed for {uri}", detail=str(exc)) from exc

    def exists(self, uri: str) -> bool:
        bucket, key = self._split(uri)
        try:
            self.client.head_object(Bucket=bucket, Key=key)
            return True
        except Exception:
            return False

    def presigned_url(self, uri: str, *, expires_in: int = 900) -> str:
        bucket, key = self._split(uri)
        try:
            return self.client.generate_presigned_url(
                "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires_in
            )
        except Exception as exc:
            raise ExternalServiceError(f"Presign failed for {uri}", detail=str(exc)) from exc


def build_object_store(settings: Settings, *, bucket: str = ""):
    if settings.object_store_backend == "s3":
        return S3ObjectStore(settings, bucket=bucket)
    return LocalObjectStore(settings)
