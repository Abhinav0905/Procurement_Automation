"""Stage 5 - requirement extraction service.

Runs the deterministic specification parser over every non-quarantined
engineering document on the case, adds material-master and requisition-derived
requirements, deduplicates, and persists the result.

The language model is a *supplement*: it is asked only for requirements the
parser did not find, and anything it returns is marked with lower confidence and
flagged for engineering review. With `LLM_BACKEND=deterministic` it contributes
nothing and the stage still produces a complete, reviewable requirement set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from procureguard.domain.enums import (
    ComparisonOperator,
    DecisionType,
    DocumentType,
    RequirementKind,
    RequirementObligation,
    TrustState,
)
from procureguard.domain.units import normalize_engineering_unit
from procureguard.infrastructure.factory import ServiceContext
from procureguard.infrastructure.llm.prompts import (
    REQUIREMENT_EXTRACTION_SYSTEM,
    REQUIREMENTS_SCHEMA,
    trusted_block,
    untrusted_block,
)
from procureguard.ingestion.parsers.spec_parser import ExtractedRequirement, SpecificationParser
from procureguard.observability import logger

log = logger(__name__)

SPEC_DOCUMENT_TYPES = (
    DocumentType.TECHNICAL_SPECIFICATION.value,
    DocumentType.ENGINEERING_DRAWING.value,
    DocumentType.DATASHEET.value,
    DocumentType.QUALITY_PLAN.value,
    DocumentType.BILL_OF_MATERIALS.value,
)


@dataclass(slots=True)
class RequirementExtractionResult:
    case_id: str
    requirements: list[dict[str, Any]] = field(default_factory=list)
    mandatory_count: int = 0
    desirable_count: int = 0
    documents_read: int = 0
    documents_quarantined: int = 0
    model_supplemented: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.requirements)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "total": self.total,
            "mandatory": self.mandatory_count,
            "desirable": self.desirable_count,
            "documents_read": self.documents_read,
            "documents_quarantined": self.documents_quarantined,
            "model_supplemented": self.model_supplemented,
            "warnings": self.warnings,
            "requirements": self.requirements,
        }


class RequirementExtractionService:
    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx
        self.parser = SpecificationParser()

    def extract_for_case(
        self, case_id: str, *, use_model: bool = True
    ) -> RequirementExtractionResult:
        result = RequirementExtractionResult(case_id=case_id)
        extracted: list[ExtractedRequirement] = []
        counter = 1

        # 1. Deterministic pass over each engineering document.
        for document, version in self.ctx.repos.documents.list_for_case(case_id):
            if document.document_type not in SPEC_DOCUMENT_TYPES:
                continue
            if version.trust_state == TrustState.QUARANTINED.value:
                result.documents_quarantined += 1
                result.warnings.append(
                    f"{document.logical_name} was quarantined by the document firewall and "
                    f"was not used for requirement extraction"
                )
                continue

            text = self._version_text(version)
            if not text.strip():
                result.warnings.append(
                    f"{document.logical_name} yielded no extractable text; requirements from "
                    f"it must be entered manually"
                )
                continue

            result.documents_read += 1
            found = self.parser.extract(
                text,
                source_location=document.logical_name,
                document_version_id=version.id,
                start_index=counter,
            )
            counter += len(found)
            for item in found:
                # Trust flows from the document, not from the parser.
                extracted.append(item)

        # 2. Requirements implied by the requisition and material master.
        derived = self._derive_from_case(case_id, start_index=counter)
        counter += len(derived)
        extracted.extend(derived)

        # 3. Optional model supplement for anything prose-shaped the parser missed.
        if use_model and result.documents_read:
            supplements = self._model_supplement(case_id, extracted, start_index=counter)
            result.model_supplemented = len(supplements)
            extracted.extend(supplements)

        deduped = _dedupe(extracted)
        payload = [self._to_row(item) for item in deduped]
        self.ctx.repos.requirements.replace_for_case(case_id, payload)

        result.requirements = payload
        result.mandatory_count = sum(
            1 for r in payload if r["obligation"] == RequirementObligation.MANDATORY.value
        )
        result.desirable_count = sum(
            1 for r in payload if r["obligation"] == RequirementObligation.DESIRABLE.value
        )
        if not payload:
            result.warnings.append(
                "No requirements could be extracted. The RFQ would carry no technical "
                "criteria, so engineering input is required before going to market."
            )

        self.ctx.repos.decisions.record(
            case_id=case_id,
            decision_type=DecisionType.REQUIREMENT_EXTRACTION.value,
            recommendation=result.to_dict(),
            rationale=(
                f"Extracted {result.total} requirements "
                f"({result.mandatory_count} mandatory) from {result.documents_read} document(s)"
            ),
            confidence=_mean(payload),
            model_metadata={
                "parser": "deterministic-spec-parser-v1",
                "model_supplemented": result.model_supplemented,
            },
            evidence=[
                {
                    "evidence_type": "DOCUMENT_VERSION",
                    "evidence_id": r["source_document_version_id"],
                    "role": "SOURCE_OF_TRUTH",
                    "excerpt": r["raw_text"][:500],
                }
                for r in payload
                if r.get("source_document_version_id")
            ][:25],
        )
        self.ctx.audit(
            entity_type="REQUIREMENT_SET",
            entity_id=case_id,
            case_id=case_id,
            action="REQUIREMENTS_EXTRACTED",
            after_state={"count": result.total, "mandatory": result.mandatory_count},
        )
        log.info(
            "requirements_extracted",
            case_id=case_id,
            total=result.total,
            mandatory=result.mandatory_count,
            documents=result.documents_read,
        )
        return result

    # ---------------------------------------------------------------- helpers
    def _version_text(self, version: Any) -> str:
        if version.extracted_text_uri:
            try:
                return self.ctx.object_store.get(version.extracted_text_uri).decode(
                    "utf-8", errors="replace"
                )
            except Exception as exc:
                log.error("extracted_text_unreadable", uri=version.extracted_text_uri, detail=str(exc)[:200])
        # Fall back to the indexed chunks, which are already sanitized.
        chunks = self.ctx.repos.chunks.list_for_version(version.id)
        return "\n".join(chunk.content for chunk in chunks)

    def _derive_from_case(self, case_id: str, *, start_index: int) -> list[ExtractedRequirement]:
        """Requirements that come from the requisition itself, not a spec.

        Delivery dates and quantities are contractual requirements even when no
        engineering document exists, and a supplier who cannot meet the date is
        non-compliant regardless of how good the part is.
        """
        case = self.ctx.repos.cases.get(case_id)
        if case is None:
            return []
        pr = self.ctx.repos.requisitions.get(case.pr_number)
        if pr is None:
            return []

        out: list[ExtractedRequirement] = []
        index = start_index
        earliest = min((line.required_date for line in pr.lines if line.required_date), default=None)
        if earliest is not None:
            out.append(
                ExtractedRequirement(
                    requirement_key=f"REQ-{index:03d}",
                    kind=RequirementKind.DELIVERY.value,
                    obligation=RequirementObligation.MANDATORY.value,
                    attribute="Delivery on or before required date",
                    operator=ComparisonOperator.PRESENT.value,
                    raw_text=f"Required delivery date {earliest.date().isoformat()}",
                    target_value=earliest.date().isoformat(),
                    weight=Decimal("3"),
                    source_location=f"PR {pr.pr_number}",
                    confidence=Decimal("1.0"),
                )
            )
            index += 1

        for line in pr.lines:
            material = (
                self.ctx.repos.materials.get(line.material_code) if line.material_code else None
            )
            if material is None:
                continue
            if material.quality_inspection_required:
                out.append(
                    ExtractedRequirement(
                        requirement_key=f"REQ-{index:03d}",
                        kind=RequirementKind.QUALITY.value,
                        obligation=RequirementObligation.MANDATORY.value,
                        attribute="Certificate of conformity with each delivery",
                        operator=ComparisonOperator.BOOLEAN.value,
                        raw_text=(
                            f"Material {material.material_code} is quality-inspection relevant"
                        ),
                        target_value="yes",
                        weight=Decimal("2"),
                        source_location=f"Material master {material.material_code}",
                        confidence=Decimal("1.0"),
                    )
                )
                index += 1
            if material.hazardous:
                out.append(
                    ExtractedRequirement(
                        requirement_key=f"REQ-{index:03d}",
                        kind=RequirementKind.DOCUMENTATION.value,
                        obligation=RequirementObligation.MANDATORY.value,
                        attribute="Safety data sheet and hazard labelling",
                        operator=ComparisonOperator.BOOLEAN.value,
                        raw_text=f"Material {material.material_code} is classified hazardous",
                        target_value="yes",
                        weight=Decimal("3"),
                        source_location=f"Material master {material.material_code}",
                        confidence=Decimal("1.0"),
                    )
                )
                index += 1
            if material.batch_controlled:
                out.append(
                    ExtractedRequirement(
                        requirement_key=f"REQ-{index:03d}",
                        kind=RequirementKind.QUALITY.value,
                        obligation=RequirementObligation.MANDATORY.value,
                        attribute="Batch identification and traceability",
                        operator=ComparisonOperator.BOOLEAN.value,
                        raw_text=f"Material {material.material_code} is batch-controlled",
                        target_value="yes",
                        weight=Decimal("2"),
                        source_location=f"Material master {material.material_code}",
                        confidence=Decimal("1.0"),
                    )
                )
                index += 1
        return out

    def _model_supplement(
        self, case_id: str, already: list[ExtractedRequirement], *, start_index: int
    ) -> list[ExtractedRequirement]:
        """Ask the model only for what the parser did not already capture."""
        chunks = self.ctx.repos.chunks.keyword_search(
            "shall must minimum maximum required tolerance certificate standard",
            case_id=case_id,
            limit=12,
        )
        if not chunks:
            return []
        known = "\n".join(f"- {r.attribute}: {r.raw_text[:120]}" for r in already[:60])
        corpus = "\n\n".join(chunk.content for chunk in chunks)[:24_000]

        prompt = (
            trusted_block(
                known or "(none extracted yet)", label="ALREADY EXTRACTED REQUIREMENTS"
            )
            + "\n\n"
            + untrusted_block(corpus, label="SPECIFICATION EXTRACT")
            + "\n\nList only requirements that are NOT already covered above. "
            "Return an empty array if there are none."
        )
        try:
            response = self.ctx.model.generate_json(
                system=REQUIREMENT_EXTRACTION_SYSTEM,
                prompt=prompt,
                schema=REQUIREMENTS_SCHEMA,
                purpose="requirement_extraction",
            )
        except Exception as exc:
            log.error("requirement_model_supplement_failed", detail=str(exc)[:300])
            return []

        payload = response.content if isinstance(response.content, dict) else {}
        out: list[ExtractedRequirement] = []
        for offset, item in enumerate(payload.get("requirements", []) or []):
            if not isinstance(item, dict) or not item.get("attribute"):
                continue
            try:
                operator = ComparisonOperator(str(item.get("operator", "EQ")).upper())
                kind = RequirementKind(str(item.get("kind", "OTHER")).upper())
                obligation = RequirementObligation(
                    str(item.get("obligation", "DESIRABLE")).upper()
                )
            except ValueError:
                continue
            out.append(
                ExtractedRequirement(
                    requirement_key=f"REQ-{start_index + offset:03d}",
                    kind=kind.value,
                    obligation=obligation.value,
                    attribute=str(item["attribute"])[:255],
                    operator=operator.value,
                    raw_text=str(item.get("raw_text", ""))[:4000],
                    target_value=str(item.get("target_value", "")),
                    target_numeric=_opt_dec(item.get("target_numeric")),
                    lower_numeric=_opt_dec(item.get("lower_numeric")),
                    upper_numeric=_opt_dec(item.get("upper_numeric")),
                    tolerance_plus=_opt_dec(item.get("tolerance_plus")),
                    tolerance_minus=_opt_dec(item.get("tolerance_minus")),
                    uom=normalize_engineering_unit(str(item.get("uom", ""))),
                    allowed_values=[str(v) for v in (item.get("allowed_values") or [])],
                    weight=_opt_dec(item.get("weight")) or Decimal(1),
                    source_location=str(item.get("source_location", "model-supplemented")),
                    # Capped below the parser's floor so a reviewer can always
                    # tell model-derived requirements from parsed ones.
                    confidence=min(
                        _opt_dec(item.get("confidence")) or Decimal("0.5"), Decimal("0.6")
                    ),
                )
            )
        return out

    @staticmethod
    def _to_row(item: ExtractedRequirement) -> dict[str, Any]:
        row = item.to_dict()
        row["pr_line_number"] = 1
        row["trust_state"] = (
            TrustState.UNVERIFIED.value
            if item.confidence < Decimal("0.8")
            else TrustState.AUTHORITATIVE.value
        )
        return row


def _dedupe(items: list[ExtractedRequirement]) -> list[ExtractedRequirement]:
    """Collapse duplicates, keeping the highest-confidence statement."""
    best: dict[tuple[str, str], ExtractedRequirement] = {}
    order: list[tuple[str, str]] = []
    for item in items:
        key = (
            _norm(item.attribute),
            item.operator,
        )
        existing = best.get(key)
        if existing is None:
            best[key] = item
            order.append(key)
            continue
        # Prefer the more binding obligation, then the higher confidence.
        rank = {"MANDATORY": 2, "DESIRABLE": 1, "INFORMATIONAL": 0}
        if (rank[item.obligation], item.confidence) > (
            rank[existing.obligation],
            existing.confidence,
        ):
            item.requirement_key = existing.requirement_key
            best[key] = item
    return [best[key] for key in order]


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def _opt_dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _mean(rows: list[dict[str, Any]]) -> Decimal:
    if not rows:
        return Decimal(0)
    total = sum(Decimal(str(r.get("extraction_confidence", 0))) for r in rows)
    return (total / len(rows)).quantize(Decimal("0.0001"))
