"""Stage 1 - requisition intake.

Takes whatever arrived (portal JSON, CSV export, forwarded email), stores the
raw artifact immutably, parses it, persists the requisition, and opens the
sourcing case. The raw bytes are written *before* parsing so that a parse
failure still leaves an auditable record of what the requester actually sent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from procureguard.domain.entities import PurchaseRequisition, SourcingCase
from procureguard.domain.enums import (
    CaseState,
    DecisionType,
    DocumentAuthority,
    DocumentType,
    TrustState,
)
from procureguard.domain.errors import ConflictError, ValidationError
from procureguard.infrastructure.factory import ServiceContext
from procureguard.infrastructure.storage.object_store import content_key, guess_media_type
from procureguard.ingestion.parsers.pr_parser import ParseResult, PurchaseRequisitionParser
from procureguard.observability import logger

log = logger(__name__)


@dataclass(slots=True)
class IntakeResult:
    case_id: str
    pr_number: str
    requisition: PurchaseRequisition
    document_version_id: str
    parse_confidence: Decimal
    warnings: list[str]
    source_format: str
    created: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "pr_number": self.pr_number,
            "document_version_id": self.document_version_id,
            "parse_confidence": str(self.parse_confidence),
            "warnings": self.warnings,
            "source_format": self.source_format,
            "line_count": len(self.requisition.lines),
            "created": self.created,
        }


class RequisitionIntakeService:
    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx
        self.parser = PurchaseRequisitionParser()

    def intake(
        self,
        *,
        content: bytes,
        filename: str = "requisition.json",
        media_type: str = "",
        source_channel: str = "API",
        case_id: str = "",
        default_plant: str = "",
        received_from: str = "",
    ) -> IntakeResult:
        settings = self.ctx.settings
        media_type = media_type or guess_media_type(filename, "text/plain")

        # 1. Persist the raw artifact first, unconditionally.
        stored = self.ctx.object_store.put(
            key=content_key(prefix="requisitions", content=content, filename=filename),
            body=content,
            content_type=media_type,
            metadata={"source_channel": source_channel, "filename": filename[:200]},
        )
        document = self.ctx.repos.documents.get_or_create_document(
            logical_name=filename or "requisition",
            document_type=DocumentType.PURCHASE_REQUISITION,
        )
        version, _created = self.ctx.repos.documents.add_version(
            document,
            content=content,
            storage_uri=stored.uri,
            media_type=media_type,
            original_filename=filename,
            authority=DocumentAuthority.PROCUREMENT,
            trust_state=TrustState.VERIFIED,
            uploaded_by=self.ctx.actor_id,
            received_from=received_from,
            metadata={"source_channel": source_channel},
        )

        # 2. Parse.
        parsed: ParseResult = self.parser.parse(
            content,
            media_type=media_type,
            filename=filename,
            source_channel=source_channel,
            default_plant=default_plant or "",
            default_currency=settings.base_currency,
        )
        pr = parsed.requisition
        if not pr.pr_number:
            pr.pr_number = self._synthesize_pr_number(stored.content_hash)
            parsed.add_warning(
                f"No PR number was present; assigned provisional number {pr.pr_number}"
            )

        errors = pr.validate()
        if not pr.lines:
            raise ValidationError(
                "Requisition contains no usable lines",
                pr_number=pr.pr_number,
                warnings=parsed.warnings,
            )

        # 3. Persist requisition and open the case.
        existing_case = self.ctx.repos.cases.get_by_pr_number(pr.pr_number)
        if existing_case is not None:
            # Re-delivery of the same document is a no-op; a *different*
            # document under the same PR number is a genuine conflict.
            requisition_row = self.ctx.repos.requisitions.get_model(pr.pr_number)
            if requisition_row and requisition_row.raw_document_version_id == version.id:
                return IntakeResult(
                    case_id=existing_case.case_id,
                    pr_number=pr.pr_number,
                    requisition=pr,
                    document_version_id=version.id,
                    parse_confidence=parsed.confidence,
                    warnings=parsed.warnings,
                    source_format=parsed.source_format,
                    created=False,
                )
            raise ConflictError(
                f"PR {pr.pr_number} already has an open case with different content",
                case_id=existing_case.case_id,
            )

        self.ctx.repos.requisitions.save(
            pr,
            raw_document_version_id=version.id,
            parse_confidence=parsed.confidence,
            parse_warnings=parsed.warnings,
            validation_errors=errors,
            estimated_value_base=pr.total_estimated_value(settings.base_currency).amount,
        )

        case_id = case_id or self._case_id_for(pr.pr_number)
        case = SourcingCase(
            case_id=case_id,
            pr_number=pr.pr_number,
            tenant_id=self.ctx.tenant_id,
            state=CaseState.RECEIVED,
            base_currency=settings.base_currency,
            estimated_value_base=pr.total_estimated_value(settings.base_currency).amount,
        )
        requisition_row = self.ctx.repos.requisitions.get_model(pr.pr_number)
        self.ctx.repos.cases.save(
            case,
            plant_code=pr.plant_code,
            title=_case_title(pr),
            requisition_id=requisition_row.id if requisition_row else "",
            buyer_id=self.ctx.actor_id if self.ctx.actor_id != "SYSTEM" else "",
            due_date=min(
                (line.required_date for line in pr.lines if line.required_date), default=None
            ),
        )
        document.case_id = case_id
        version.metadata_json = {**(version.metadata_json or {}), "case_id": case_id}

        self.ctx.repos.decisions.record(
            case_id=case_id,
            decision_type=DecisionType.PR_VALIDATION.value,
            recommendation={
                "pr_number": pr.pr_number,
                "lines": len(pr.lines),
                "source_format": parsed.source_format,
                "warnings": parsed.warnings,
                "validation_errors": errors,
            },
            rationale=(
                f"Parsed {len(pr.lines)} line(s) from a {parsed.source_format} requisition "
                f"with confidence {parsed.confidence}"
            ),
            confidence=parsed.confidence,
            model_metadata={"parser": "deterministic-pr-parser-v1"},
            evidence=[
                {
                    "evidence_type": "DOCUMENT_VERSION",
                    "evidence_id": version.id,
                    "evidence_version": version.content_hash,
                    "role": "SOURCE_OF_TRUTH",
                }
            ],
        )
        self.ctx.audit(
            entity_type="SOURCING_CASE",
            entity_id=case_id,
            case_id=case_id,
            action="CASE_OPENED",
            after_state={"pr_number": pr.pr_number, "state": str(CaseState.RECEIVED)},
            detail=f"Requisition intake from {source_channel}",
        )
        log.info(
            "requisition_intake",
            case_id=case_id,
            pr_number=pr.pr_number,
            lines=len(pr.lines),
            source_format=parsed.source_format,
            confidence=str(parsed.confidence),
        )
        return IntakeResult(
            case_id=case_id,
            pr_number=pr.pr_number,
            requisition=pr,
            document_version_id=version.id,
            parse_confidence=parsed.confidence,
            warnings=parsed.warnings,
            source_format=parsed.source_format,
            created=True,
        )

    @staticmethod
    def _case_id_for(pr_number: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9]+", "-", pr_number).strip("-").upper()
        return f"PG-{slug}"[:64]

    @staticmethod
    def _synthesize_pr_number(content_hash: str) -> str:
        stamp = datetime.now(UTC).strftime("%Y%m")
        return f"PR-{stamp}-{content_hash[:8].upper()}"


def _case_title(pr: PurchaseRequisition) -> str:
    if not pr.lines:
        return pr.pr_number
    first = pr.lines[0]
    label = first.description or first.material_code or "requisition"
    suffix = f" (+{len(pr.lines) - 1} more)" if len(pr.lines) > 1 else ""
    return f"{label}{suffix}"[:500]
