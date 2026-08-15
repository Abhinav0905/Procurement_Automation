"""Evidence repositories: documents, versions, chunks, claims, findings.

Nothing in this module updates content. A "change" is always a new version or a
new claim that supersedes an older one, because an evaluation that cannot be
replayed against the exact bytes it saw is not auditable.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, func, or_, select
from sqlalchemy.exc import IntegrityError

from procureguard.domain.enums import DocumentAuthority, DocumentType, TrustState
from procureguard.observability import logger

from .models import (
    ClaimConflictModel,
    ClaimModel,
    DocumentChunkModel,
    DocumentModel,
    DocumentVersionModel,
    SecurityFindingModel,
    utcnow,
)
from .repo_core import TenantScopedRepository
from .vector import VectorSearch

log = logger(__name__)


class SqlDocumentRepository(TenantScopedRepository):
    def get_or_create_document(
        self,
        *,
        logical_name: str,
        document_type: DocumentType | str,
        case_id: str = "",
        material_code: str = "",
        vendor_id: str = "",
    ) -> DocumentModel:
        doc_type = str(document_type)
        existing = self.session.scalars(
            self._scoped(select(DocumentModel), DocumentModel).where(
                DocumentModel.logical_name == logical_name,
                DocumentModel.document_type == doc_type,
                DocumentModel.case_id == case_id,
            )
        ).first()
        if existing:
            return existing
        row = DocumentModel(
            tenant_id=self.tenant_id,
            logical_name=logical_name[:255],
            document_type=doc_type,
            case_id=case_id,
            material_code=material_code,
            vendor_id=vendor_id,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def add_version(
        self,
        document: DocumentModel,
        *,
        content: bytes | None = None,
        content_hash: str = "",
        storage_uri: str,
        media_type: str = "application/octet-stream",
        original_filename: str = "",
        authority: DocumentAuthority | str = DocumentAuthority.UNKNOWN,
        trust_state: TrustState | str = TrustState.UNVERIFIED,
        uploaded_by: str = "",
        received_from: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> tuple[DocumentVersionModel, bool]:
        """Add an immutable version.

        Returns (version, created). Re-uploading identical bytes returns the
        existing version rather than duplicating storage or re-running
        extraction - documents genuinely do arrive twice.
        """
        digest = content_hash or (hashlib.sha256(content).hexdigest() if content else "")
        if not digest:
            raise ValueError("A document version needs either content or a content hash")

        existing = self.session.scalars(
            self._scoped(select(DocumentVersionModel), DocumentVersionModel).where(
                DocumentVersionModel.content_hash == digest
            )
        ).first()
        if existing:
            return existing, False

        version_count = int(
            self.session.scalar(
                select(func.count())
                .select_from(DocumentVersionModel)
                .where(DocumentVersionModel.document_id == document.id)
            )
            or 0
        )
        previous = self.current_version(document.id)
        row = DocumentVersionModel(
            tenant_id=self.tenant_id,
            document_id=document.id,
            version_label=str(version_count + 1),
            content_hash=digest,
            storage_uri=storage_uri,
            media_type=media_type,
            byte_size=len(content) if content else 0,
            original_filename=original_filename[:500],
            authority=str(authority),
            trust_state=str(trust_state),
            uploaded_by=uploaded_by,
            received_from=received_from,
            metadata_json=metadata or {},
            supersedes_version_id=previous.id if previous else None,
        )
        self.session.add(row)
        try:
            self.session.flush()
        except IntegrityError:
            self.session.rollback()
            existing = self.session.scalars(
                self._scoped(select(DocumentVersionModel), DocumentVersionModel).where(
                    DocumentVersionModel.content_hash == digest
                )
            ).first()
            if existing:
                return existing, False
            raise
        document.current_version_id = row.id
        if previous is not None:
            previous.trust_state = TrustState.SUPERSEDED.value
        self.session.flush()
        return row, True

    def get_version(self, version_id: str) -> DocumentVersionModel | None:
        row = self.session.get(DocumentVersionModel, version_id)
        return row if row and row.tenant_id == self.tenant_id else None

    def current_version(self, document_id: str) -> DocumentVersionModel | None:
        return self.session.scalars(
            self._scoped(select(DocumentVersionModel), DocumentVersionModel)
            .where(DocumentVersionModel.document_id == document_id)
            .order_by(desc(DocumentVersionModel.created_at))
            .limit(1)
        ).first()

    def list_for_case(
        self, case_id: str, *, document_type: str | None = None
    ) -> list[tuple[DocumentModel, DocumentVersionModel]]:
        stmt = (
            self._scoped(select(DocumentModel, DocumentVersionModel), DocumentModel)
            .join(DocumentVersionModel, DocumentVersionModel.document_id == DocumentModel.id)
            .where(DocumentModel.case_id == case_id)
        )
        if document_type:
            stmt = stmt.where(DocumentModel.document_type == document_type)
        return [
            (row[0], row[1])
            for row in self.session.execute(
                stmt.order_by(desc(DocumentVersionModel.created_at))
            ).all()
        ]

    def record_extraction(
        self,
        version: DocumentVersionModel,
        *,
        extracted_text_uri: str,
        char_count: int,
        page_count: int,
        method: str,
    ) -> None:
        version.extracted_text_uri = extracted_text_uri
        version.extracted_char_count = char_count
        version.page_count = page_count
        version.extraction_method = method
        self.session.flush()

    def record_firewall_verdict(
        self, version: DocumentVersionModel, *, verdict: str, findings: Sequence[dict[str, Any]]
    ) -> None:
        version.firewall_verdict = verdict
        version.firewall_findings = list(findings)
        if verdict == "QUARANTINE":
            version.trust_state = TrustState.QUARANTINED.value
        self.session.flush()


class SqlChunkRepository(TenantScopedRepository):
    def add_chunks(
        self,
        *,
        document_version_id: str,
        case_id: str,
        chunks: Sequence[dict[str, Any]],
        authority: str,
        trust_state: str,
        embedding_model: str = "",
    ) -> int:
        existing = int(
            self.session.scalar(
                select(func.count())
                .select_from(DocumentChunkModel)
                .where(DocumentChunkModel.document_version_id == document_version_id)
            )
            or 0
        )
        if existing:
            return 0  # already chunked; extraction is idempotent
        for index, chunk in enumerate(chunks):
            content = str(chunk.get("content", ""))
            self.session.add(
                DocumentChunkModel(
                    tenant_id=self.tenant_id,
                    document_version_id=document_version_id,
                    case_id=case_id,
                    chunk_index=index,
                    content=content,
                    content_hash=hashlib.sha256(content.encode()).hexdigest(),
                    token_estimate=int(chunk.get("token_estimate", len(content) // 4)),
                    page_number=int(chunk.get("page_number", 0)),
                    section_path=str(chunk.get("section_path", ""))[:500],
                    authority=authority,
                    trust_state=trust_state,
                    embedding=chunk.get("embedding"),
                    embedding_model=embedding_model,
                )
            )
        self.session.flush()
        return len(chunks)

    def get(self, chunk_id: str) -> DocumentChunkModel | None:
        row = self.session.get(DocumentChunkModel, chunk_id)
        return row if row and row.tenant_id == self.tenant_id else None

    def list_for_version(self, document_version_id: str) -> list[DocumentChunkModel]:
        return list(
            self.session.scalars(
                select(DocumentChunkModel)
                .where(DocumentChunkModel.document_version_id == document_version_id)
                .order_by(DocumentChunkModel.chunk_index)
            ).all()
        )

    def semantic_search(
        self,
        query_vector: Sequence[float],
        *,
        case_id: str = "",
        top_k: int = 8,
        dimensions: int = 1024,
        exclude_quarantined: bool = True,
        candidate_limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Retrieve evidence chunks by meaning.

        Quarantined content is excluded by default: a chunk that failed the
        firewall must never silently become context for a decision.
        """
        clauses = ["tenant_id = :tenant_id", "embedding IS NOT NULL"]
        params: dict[str, Any] = {"tenant_id": self.tenant_id}
        if case_id:
            clauses.append("case_id = :case_id")
            params["case_id"] = case_id
        if exclude_quarantined:
            clauses.append("trust_state <> 'QUARANTINED'")

        searcher = VectorSearch(dimensions=dimensions, candidate_limit=candidate_limit)
        hits = searcher.search(
            self.session.connection(),
            table="document_chunks",
            id_column="id",
            embedding_column="embedding",
            query_vector=query_vector,
            top_k=top_k,
            where_sql=" AND ".join(clauses),
            params=params,
            payload_columns=(
                "content",
                "document_version_id",
                "page_number",
                "section_path",
                "authority",
                "trust_state",
            ),
        )
        return [
            {
                "chunk_id": hit.row_id,
                "score": round(hit.score, 6),
                "content": hit.payload.get("content", ""),
                "document_version_id": hit.payload.get("document_version_id", ""),
                "page_number": hit.payload.get("page_number", 0),
                "section_path": hit.payload.get("section_path", ""),
                "authority": hit.payload.get("authority", ""),
                "trust_state": hit.payload.get("trust_state", ""),
            }
            for hit in hits
        ]

    def keyword_search(
        self, query: str, *, case_id: str = "", limit: int = 8
    ) -> list[DocumentChunkModel]:
        tokens = [t for t in query.lower().split() if len(t) >= 3][:5]
        if not tokens:
            return []
        stmt = self._scoped(select(DocumentChunkModel), DocumentChunkModel).where(
            or_(*[DocumentChunkModel.content.ilike(f"%{t}%") for t in tokens]),
            DocumentChunkModel.trust_state != TrustState.QUARANTINED.value,
        )
        if case_id:
            stmt = stmt.where(DocumentChunkModel.case_id == case_id)
        return list(self.session.scalars(stmt.limit(limit)).all())


class SqlClaimRepository(TenantScopedRepository):
    def add(
        self,
        *,
        subject: str,
        predicate: str,
        value_text: str,
        case_id: str = "",
        normalized_value: str = "",
        numeric_value: Decimal | None = None,
        uom: str = "",
        authority: str = "UNKNOWN",
        trust_state: str = "UNVERIFIED",
        confidence: Decimal | float = 0,
        document_version_id: str = "",
        chunk_id: str = "",
        source_location: str = "",
        source_excerpt: str = "",
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        extracted_by: str = "",
    ) -> ClaimModel:
        content_hash = hashlib.sha256(
            "|".join(
                [
                    self.tenant_id,
                    case_id,
                    subject.strip().lower(),
                    predicate.strip().lower(),
                    (normalized_value or value_text).strip().lower(),
                    document_version_id,
                    source_location,
                ]
            ).encode()
        ).hexdigest()

        existing = self.session.scalars(
            self._scoped(select(ClaimModel), ClaimModel).where(
                ClaimModel.content_hash == content_hash
            )
        ).first()
        if existing:
            return existing

        superseded = self._find_superseded(case_id, subject, predicate, document_version_id)
        row = ClaimModel(
            tenant_id=self.tenant_id,
            case_id=case_id,
            subject=subject[:255],
            predicate=predicate[:255],
            value_text=value_text,
            normalized_value=normalized_value or value_text,
            numeric_value=numeric_value,
            uom=uom[:16],
            authority=authority,
            trust_state=trust_state,
            confidence=Decimal(str(confidence)),
            document_version_id=document_version_id,
            chunk_id=chunk_id,
            source_location=source_location[:255],
            source_excerpt=source_excerpt[:4000],
            valid_from=valid_from,
            valid_to=valid_to,
            supersedes_claim_id=superseded.id if superseded else None,
            content_hash=content_hash,
            extracted_by=extracted_by,
        )
        self.session.add(row)
        self.session.flush()
        if superseded is not None:
            superseded.trust_state = TrustState.SUPERSEDED.value
            superseded.valid_to = superseded.valid_to or utcnow()
            self.session.flush()
        return row

    def _find_superseded(
        self, case_id: str, subject: str, predicate: str, document_version_id: str
    ) -> ClaimModel | None:
        """Only a newer version of the *same document* supersedes silently.

        A different source that disagrees is a conflict, not a replacement.
        """
        if not document_version_id:
            return None
        return self.session.scalars(
            self._scoped(select(ClaimModel), ClaimModel)
            .where(
                ClaimModel.case_id == case_id,
                ClaimModel.subject == subject,
                ClaimModel.predicate == predicate,
                ClaimModel.document_version_id == document_version_id,
                ClaimModel.trust_state != TrustState.SUPERSEDED.value,
            )
            .order_by(desc(ClaimModel.learned_at))
            .limit(1)
        ).first()

    def find(
        self,
        *,
        subject: str = "",
        predicate: str = "",
        case_id: str = "",
        include_superseded: bool = False,
        limit: int = 200,
    ) -> list[ClaimModel]:
        stmt = self._scoped(select(ClaimModel), ClaimModel)
        if subject:
            stmt = stmt.where(ClaimModel.subject == subject)
        if predicate:
            stmt = stmt.where(ClaimModel.predicate == predicate)
        if case_id:
            stmt = stmt.where(ClaimModel.case_id == case_id)
        if not include_superseded:
            stmt = stmt.where(
                ClaimModel.trust_state.notin_(
                    [TrustState.SUPERSEDED.value, TrustState.QUARANTINED.value]
                )
            )
        return list(
            self.session.scalars(stmt.order_by(desc(ClaimModel.learned_at)).limit(limit)).all()
        )

    def detect_conflicts(self, case_id: str) -> list[ClaimConflictModel]:
        """Flag disagreeing claims about the same subject+predicate.

        Authoritative sources win over supplier assertions automatically; two
        sources of equal standing produce an unresolved conflict for a human.
        """
        claims = self.find(case_id=case_id, limit=5_000)
        grouped: dict[tuple[str, str], list[ClaimModel]] = {}
        for claim in claims:
            grouped.setdefault((claim.subject, claim.predicate), []).append(claim)

        created: list[ClaimConflictModel] = []
        for (subject, predicate), group in grouped.items():
            if len(group) < 2:
                continue
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    a, b = group[i], group[j]
                    if _same_value(a, b):
                        continue
                    if self._conflict_exists(a.id, b.id):
                        continue
                    resolution, resolved_id = _auto_resolve(a, b)
                    row = ClaimConflictModel(
                        tenant_id=self.tenant_id,
                        case_id=case_id,
                        subject=subject,
                        predicate=predicate,
                        claim_a_id=a.id,
                        claim_b_id=b.id,
                        conflict_type="VALUE_MISMATCH",
                        severity="HIGH" if resolution == "UNRESOLVED" else "LOW",
                        detail=(
                            f"{a.authority} asserts {a.normalized_value!r}; "
                            f"{b.authority} asserts {b.normalized_value!r}"
                        ),
                        resolution=resolution,
                        resolved_claim_id=resolved_id,
                    )
                    self.session.add(row)
                    created.append(row)
        self.session.flush()
        return created

    def _conflict_exists(self, claim_a: str, claim_b: str) -> bool:
        return (
            self.session.scalars(
                self._scoped(select(ClaimConflictModel), ClaimConflictModel).where(
                    or_(
                        (ClaimConflictModel.claim_a_id == claim_a)
                        & (ClaimConflictModel.claim_b_id == claim_b),
                        (ClaimConflictModel.claim_a_id == claim_b)
                        & (ClaimConflictModel.claim_b_id == claim_a),
                    )
                )
            ).first()
            is not None
        )

    def unresolved_conflicts(self, case_id: str) -> list[ClaimConflictModel]:
        return list(
            self.session.scalars(
                self._scoped(select(ClaimConflictModel), ClaimConflictModel).where(
                    ClaimConflictModel.case_id == case_id,
                    ClaimConflictModel.resolution == "UNRESOLVED",
                )
            ).all()
        )


class SqlSecurityFindingRepository(TenantScopedRepository):
    def record_many(
        self,
        findings: Sequence[Any],
        *,
        case_id: str = "",
        document_version_id: str = "",
        communication_id: str = "",
    ) -> list[SecurityFindingModel]:
        rows: list[SecurityFindingModel] = []
        for finding in findings:
            row = SecurityFindingModel(
                tenant_id=self.tenant_id,
                case_id=case_id,
                document_version_id=document_version_id,
                communication_id=communication_id,
                finding_type=getattr(finding, "finding_type", str(finding))[:80],
                severity=getattr(finding, "severity", "MEDIUM"),
                detail=getattr(finding, "detail", "")[:8000],
                matched_excerpt=getattr(finding, "matched_excerpt", "")[:4000],
                disposition=getattr(finding, "disposition", ""),
            )
            self.session.add(row)
            rows.append(row)
        self.session.flush()
        return rows

    def list_for_case(self, case_id: str, *, min_severity: str = "") -> list[SecurityFindingModel]:
        order = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
        rows = list(
            self.session.scalars(
                self._scoped(select(SecurityFindingModel), SecurityFindingModel)
                .where(SecurityFindingModel.case_id == case_id)
                .order_by(desc(SecurityFindingModel.created_at))
            ).all()
        )
        if min_severity:
            threshold = order.get(min_severity.upper(), 0)
            rows = [r for r in rows if order.get(r.severity, 0) >= threshold]
        return rows

    def has_unacknowledged_critical(self, case_id: str) -> bool:
        return (
            self.session.scalars(
                self._scoped(select(SecurityFindingModel), SecurityFindingModel).where(
                    SecurityFindingModel.case_id == case_id,
                    SecurityFindingModel.severity == "CRITICAL",
                    SecurityFindingModel.acknowledged_at.is_(None),
                )
            ).first()
            is not None
        )

    def acknowledge(self, finding_id: str, actor_id: str) -> None:
        row = self.session.get(SecurityFindingModel, finding_id)
        if row is None:
            return
        row.acknowledged_by = actor_id
        row.acknowledged_at = utcnow()
        self.session.flush()


_AUTHORITY_RANK = {
    "ERP_MASTER": 5,
    "ENGINEERING": 4,
    "QUALITY": 4,
    "THIRD_PARTY_CERT": 3,
    "PROCUREMENT": 2,
    "SUPPLIER": 1,
    "UNKNOWN": 0,
}


def _same_value(a: ClaimModel, b: ClaimModel) -> bool:
    if a.numeric_value is not None and b.numeric_value is not None:
        return Decimal(str(a.numeric_value)) == Decimal(str(b.numeric_value)) and a.uom == b.uom
    return (a.normalized_value or "").strip().lower() == (b.normalized_value or "").strip().lower()


def _auto_resolve(a: ClaimModel, b: ClaimModel) -> tuple[str, str]:
    rank_a = _AUTHORITY_RANK.get(a.authority, 0)
    rank_b = _AUTHORITY_RANK.get(b.authority, 0)
    if rank_a > rank_b:
        return "RESOLVED_BY_AUTHORITY", a.id
    if rank_b > rank_a:
        return "RESOLVED_BY_AUTHORITY", b.id
    return "UNRESOLVED", ""
