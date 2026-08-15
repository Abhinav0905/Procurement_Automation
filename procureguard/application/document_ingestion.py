"""Stage 4 - engineering document ingestion.

The pipeline is fixed and its order is the security control:

    store raw bytes -> extract text -> FIREWALL -> chunk -> embed -> index

Extraction happens before the firewall because you cannot scan a PDF you have
not read; indexing happens after, because quarantined content must never become
retrievable context. A quarantined document is still stored and still visible to
a human - it is only the *agent* that is denied it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from procureguard.domain.enums import DocumentAuthority, DocumentType, TrustState
from procureguard.infrastructure.factory import ServiceContext
from procureguard.infrastructure.storage.object_store import content_key, guess_media_type
from procureguard.ingestion.parsers.text_extract import TextExtractor, chunk_text
from procureguard.observability import METRICS, logger
from procureguard.security.document_firewall import Verdict

log = logger(__name__)


@dataclass(slots=True)
class IngestionResult:
    document_id: str
    document_version_id: str
    storage_uri: str
    content_hash: str
    already_ingested: bool
    extraction_method: str
    char_count: int
    page_count: int
    chunk_count: int
    embedded: bool
    firewall_verdict: str
    findings: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_quarantined(self) -> bool:
        return self.firewall_verdict == Verdict.QUARANTINE.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "document_version_id": self.document_version_id,
            "storage_uri": self.storage_uri,
            "content_hash": self.content_hash,
            "already_ingested": self.already_ingested,
            "extraction_method": self.extraction_method,
            "char_count": self.char_count,
            "page_count": self.page_count,
            "chunk_count": self.chunk_count,
            "embedded": self.embedded,
            "firewall_verdict": self.firewall_verdict,
            "findings": self.findings,
            "warnings": self.warnings,
        }


class DocumentIngestionService:
    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx
        self.extractor = TextExtractor()

    def ingest(
        self,
        *,
        content: bytes,
        filename: str,
        case_id: str = "",
        document_type: DocumentType | str = DocumentType.TECHNICAL_SPECIFICATION,
        authority: DocumentAuthority | str = DocumentAuthority.ENGINEERING,
        media_type: str = "",
        material_code: str = "",
        vendor_id: str = "",
        received_from: str = "",
        embed: bool = True,
    ) -> IngestionResult:
        media_type = media_type or guess_media_type(filename)
        authority_enum = (
            authority if isinstance(authority, DocumentAuthority) else DocumentAuthority(str(authority))
        )

        # 1. Store raw bytes, unconditionally and before interpretation.
        stored = self.ctx.object_store.put(
            key=content_key(prefix="documents", content=content, filename=filename),
            body=content,
            content_type=media_type,
            metadata={"case_id": case_id, "document_type": str(document_type)},
        )
        document = self.ctx.repos.documents.get_or_create_document(
            logical_name=filename,
            document_type=document_type,
            case_id=case_id,
            material_code=material_code,
            vendor_id=vendor_id,
        )
        version, created = self.ctx.repos.documents.add_version(
            document,
            content=content,
            storage_uri=stored.uri,
            media_type=media_type,
            original_filename=filename,
            authority=authority_enum,
            trust_state=(
                TrustState.UNVERIFIED
                if authority_enum.is_supplier_controlled
                else TrustState.AUTHORITATIVE
            ),
            uploaded_by=self.ctx.actor_id,
            received_from=received_from,
            metadata={"case_id": case_id},
        )
        if not created:
            # Identical bytes already ingested: extraction, chunking and
            # embedding were done once and must not be repeated.
            log.info("document_already_ingested", version_id=version.id, filename=filename)
            return IngestionResult(
                document_id=document.id,
                document_version_id=version.id,
                storage_uri=version.storage_uri,
                content_hash=version.content_hash,
                already_ingested=True,
                extraction_method=version.extraction_method,
                char_count=version.extracted_char_count,
                page_count=version.page_count,
                chunk_count=len(self.ctx.repos.chunks.list_for_version(version.id)),
                embedded=True,
                firewall_verdict=version.firewall_verdict,
                findings=list(version.firewall_findings or []),
            )

        # 2. Extract text.
        extracted = self.extractor.extract(content, media_type=media_type, filename=filename)
        text_uri = ""
        if extracted.text:
            text_stored = self.ctx.object_store.put(
                key=f"extracted/{version.content_hash[:2]}/{version.content_hash}.txt",
                body=extracted.text.encode("utf-8"),
                content_type="text/plain; charset=utf-8",
            )
            text_uri = text_stored.uri
        self.ctx.repos.documents.record_extraction(
            version,
            extracted_text_uri=text_uri,
            char_count=len(extracted.text),
            page_count=extracted.page_count,
            method=extracted.method,
        )

        # 3. Firewall. Supplier-controlled documents are scanned; internal
        #    engineering masters are still scanned, because a compromised
        #    internal mailbox is a real threat too.
        scan = self.ctx.firewall.scan(
            extracted.text, source_label=f"{document_type} ({filename})"
        )
        self.ctx.repos.documents.record_firewall_verdict(
            version,
            verdict=scan.verdict.value,
            findings=[f.to_dict() for f in scan.findings],
        )
        if scan.findings:
            self.ctx.repos.findings.record_many(
                scan.findings, case_id=case_id, document_version_id=version.id
            )
            METRICS.increment("firewall.findings", verdict=scan.verdict.value)

        # 4/5. Chunk, embed and index - only if the firewall allows it.
        chunk_count = 0
        embedded = False
        if scan.verdict == Verdict.QUARANTINE:
            log.warning(
                "document_quarantined",
                version_id=version.id,
                filename=filename,
                findings=sorted(scan.finding_types),
            )
        elif extracted.text.strip():
            # Index the sanitized text: any span the firewall marked for
            # stripping is replaced before it can be retrieved as evidence.
            indexable = scan.sanitized_text or extracted.text
            chunks = chunk_text(indexable)
            payload: list[dict[str, Any]] = []
            for chunk in chunks:
                item: dict[str, Any] = {
                    "content": chunk.content,
                    "token_estimate": chunk.token_estimate,
                    "page_number": chunk.page_number,
                    "section_path": chunk.section_path,
                }
                payload.append(item)

            if embed and payload:
                try:
                    vectors = self.ctx.embedder.embed_batch([c["content"] for c in payload])
                    for item, vector in zip(payload, vectors, strict=True):
                        item["embedding"] = vector
                    embedded = True
                except Exception as exc:
                    # Retrieval degrades to keyword search rather than failing
                    # the whole ingestion.
                    log.error("embedding_failed", version_id=version.id, detail=str(exc)[:300])

            chunk_count = self.ctx.repos.chunks.add_chunks(
                document_version_id=version.id,
                case_id=case_id,
                chunks=payload,
                authority=str(authority_enum),
                trust_state=version.trust_state,
                embedding_model=getattr(self.ctx.embedder, "model_id", ""),
            )

        self.ctx.audit(
            entity_type="DOCUMENT_VERSION",
            entity_id=version.id,
            case_id=case_id,
            action="DOCUMENT_INGESTED",
            after_state={
                "filename": filename,
                "document_type": str(document_type),
                "firewall_verdict": scan.verdict.value,
                "chunks": chunk_count,
            },
            detail=f"Ingested {filename} ({len(content)} bytes) via {extracted.method}",
        )
        log.info(
            "document_ingested",
            case_id=case_id,
            version_id=version.id,
            filename=filename,
            chunks=chunk_count,
            verdict=scan.verdict.value,
        )
        return IngestionResult(
            document_id=document.id,
            document_version_id=version.id,
            storage_uri=stored.uri,
            content_hash=stored.content_hash,
            already_ingested=False,
            extraction_method=extracted.method,
            char_count=len(extracted.text),
            page_count=extracted.page_count,
            chunk_count=chunk_count,
            embedded=embedded,
            firewall_verdict=scan.verdict.value,
            findings=[f.to_dict() for f in scan.findings],
            warnings=extracted.warnings + list(scan.normalization_notes),
        )

    # ------------------------------------------------------------- retrieval
    def retrieve(
        self, *, query: str, case_id: str = "", top_k: int = 8
    ) -> list[dict[str, Any]]:
        """Hybrid retrieval over case evidence.

        Vector recall plus keyword precision, merged by reciprocal rank. Pure
        vector search misses exact part numbers and standard designations,
        which is exactly what technical evaluation needs to find.
        """
        results: dict[str, dict[str, Any]] = {}

        try:
            vector = self.ctx.embedder.embed(query)
            for rank, hit in enumerate(
                self.ctx.repos.chunks.semantic_search(
                    vector,
                    case_id=case_id,
                    top_k=top_k * 2,
                    dimensions=self.ctx.embedder.dimensions,
                ),
                start=1,
            ):
                hit["rrf"] = 1.0 / (60 + rank)
                hit["match"] = "semantic"
                results[hit["chunk_id"]] = hit
        except Exception as exc:
            log.info("semantic_retrieval_unavailable", detail=str(exc)[:200])

        for rank, chunk in enumerate(
            self.ctx.repos.chunks.keyword_search(query, case_id=case_id, limit=top_k * 2), start=1
        ):
            existing = results.get(chunk.id)
            if existing:
                existing["rrf"] += 1.0 / (60 + rank)
                existing["match"] = "hybrid"
                continue
            results[chunk.id] = {
                "chunk_id": chunk.id,
                "score": 0.0,
                "rrf": 1.0 / (60 + rank),
                "match": "keyword",
                "content": chunk.content,
                "document_version_id": chunk.document_version_id,
                "page_number": chunk.page_number,
                "section_path": chunk.section_path,
                "authority": chunk.authority,
                "trust_state": chunk.trust_state,
            }

        ranked = sorted(results.values(), key=lambda item: item["rrf"], reverse=True)
        return ranked[:top_k]
