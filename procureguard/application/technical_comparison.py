"""Stage 10 - technical comparison engine.

Builds the requirement x supplier compliance matrix.

The division of labour is the important part. Locating what a supplier offered
is an extraction problem, and extraction may use retrieval or a model. Deciding
whether the offered value complies is arithmetic, and it is done by
`Requirement.evaluate()` with audited unit conversion. A model is never asked
"is this compliant?", because that is the question it is most likely to answer
agreeably and most expensive to get wrong.

Silence is never compliance. A requirement the supplier did not address is
NOT_ADDRESSED, and a mandatory NOT_ADDRESSED disqualifies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from procureguard.domain.entities import ComplianceAssessment, Requirement
from procureguard.domain.enums import (
    ComparisonOperator,
    ComplianceStatus,
    DecisionType,
    RequirementKind,
    RequirementObligation,
    TrustState,
)
from procureguard.domain.units import UnitConverter, normalize_engineering_unit
from procureguard.infrastructure.factory import ServiceContext
from procureguard.infrastructure.llm.prompts import (
    COMPLIANCE_ASSESSMENT_SYSTEM,
    COMPLIANCE_SCHEMA,
    trusted_block,
    untrusted_block,
)
from procureguard.observability import logger

log = logger(__name__)

_NUMERIC = re.compile(r"(?P<num>-?\d+(?:[.,]\d+)?)\s*(?P<uom>[A-Za-zµ°%/]{0,12})")

# Words suppliers use to mean "yes, we comply" without stating a value.
_AFFIRMATIVE = (
    "comply", "complies", "compliant", "confirmed", "confirm", "yes", "agreed",
    "accepted", "noted and accepted", "as per spec", "as specified", "included",
)
_NEGATIVE = ("not comply", "non-compliant", "deviation", "not available", "cannot", "unable", "no")


@dataclass(slots=True)
class SupplierEvaluation:
    vendor_id: str
    vendor_name: str
    quotation_id: str
    qualified: bool
    technical_score: Decimal
    mandatory_met: int
    mandatory_total: int
    desirable_met: int
    desirable_total: int
    blockers: list[str] = field(default_factory=list)
    deviations: list[dict[str, Any]] = field(default_factory=list)
    unanswered: list[str] = field(default_factory=list)
    assessments: list[ComplianceAssessment] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "vendor_id": self.vendor_id,
            "vendor_name": self.vendor_name,
            "quotation_id": self.quotation_id,
            "qualified": self.qualified,
            "technical_score": str(self.technical_score),
            "mandatory_met": self.mandatory_met,
            "mandatory_total": self.mandatory_total,
            "desirable_met": self.desirable_met,
            "desirable_total": self.desirable_total,
            "blockers": self.blockers,
            "deviations": self.deviations,
            "unanswered": self.unanswered,
        }


@dataclass(slots=True)
class ComparisonMatrix:
    case_id: str
    requirements: list[dict[str, Any]] = field(default_factory=list)
    evaluations: list[SupplierEvaluation] = field(default_factory=list)
    cells: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def qualified_vendor_ids(self) -> list[str]:
        return [e.vendor_id for e in self.evaluations if e.qualified]

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "requirements": self.requirements,
            "evaluations": [e.to_dict() for e in self.evaluations],
            "cells": self.cells,
            "qualified_vendor_ids": self.qualified_vendor_ids,
            "warnings": self.warnings,
        }


class TechnicalComparisonService:
    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx

    def evaluate_case(self, case_id: str, *, use_model: bool = True) -> ComparisonMatrix:
        matrix = ComparisonMatrix(case_id=case_id)
        requirement_rows = self.ctx.repos.requirements.list_active(case_id)
        if not requirement_rows:
            matrix.warnings.append(
                "No active requirements: a technical evaluation cannot be performed and every "
                "bid would qualify by default. Engineering input is required."
            )

        requirements = [_to_domain(row) for row in requirement_rows]
        matrix.requirements = [
            {
                "requirement_id": row.id,
                "requirement_key": row.requirement_key,
                "attribute": row.attribute,
                "obligation": row.obligation,
                "kind": row.kind,
                "operator": row.operator,
                "description": domain_requirement.describe(),
                "weight": str(row.weight),
            }
            for row, domain_requirement in zip(requirement_rows, requirements, strict=True)
        ]

        # Metadata-only reads: the commercial envelope stays sealed throughout.
        quotations = self.ctx.repos.quotations.list_for_case(case_id, commercial_unlocked=False)
        for quotation in quotations:
            if quotation.status in ("WITHDRAWN", "SUPERSEDED", "QUARANTINED"):
                continue
            evaluation = self._evaluate_supplier(
                case_id, quotation, requirement_rows, requirements, use_model=use_model
            )
            matrix.evaluations.append(evaluation)
            for assessment in evaluation.assessments:
                matrix.cells.setdefault(assessment.requirement_id, {})[quotation.vendor_id] = {
                    "status": str(assessment.status),
                    "offered_value": assessment.offered_value,
                    "rationale": assessment.rationale,
                    "confidence": str(assessment.confidence),
                    "deviation_accepted": assessment.deviation_accepted,
                }

        matrix.evaluations.sort(key=lambda e: (e.qualified, e.technical_score), reverse=True)

        if not matrix.qualified_vendor_ids and matrix.evaluations:
            matrix.warnings.append(
                "No supplier meets every mandatory requirement. Either accept specific "
                "deviations, relax a requirement, or re-tender."
            )

        self.ctx.repos.decisions.record(
            case_id=case_id,
            decision_type=DecisionType.TECHNICAL_COMPARISON.value,
            recommendation=matrix.to_dict(),
            rationale=(
                f"{len(matrix.qualified_vendor_ids)} of {len(matrix.evaluations)} supplier(s) "
                f"meet all {sum(1 for r in requirements if r.is_mandatory)} mandatory requirements"
            ),
            confidence=_matrix_confidence(matrix),
            model_metadata={
                "engine": "deterministic-compliance-evaluator-v1",
                "model_assisted_extraction": use_model,
            },
            evidence=[
                {
                    "evidence_type": "QUOTATION",
                    "evidence_id": e.quotation_id,
                    "role": "SUPPORTS" if e.qualified else "CONTRADICTS",
                    "excerpt": "; ".join(e.blockers)[:500],
                }
                for e in matrix.evaluations
            ],
        )
        self.ctx.audit(
            entity_type="TECHNICAL_EVALUATION",
            entity_id=case_id,
            case_id=case_id,
            action="TECHNICAL_COMPARISON_COMPLETED",
            after_state={
                "qualified": matrix.qualified_vendor_ids,
                "evaluated": len(matrix.evaluations),
            },
        )
        log.info(
            "technical_comparison_completed",
            case_id=case_id,
            evaluated=len(matrix.evaluations),
            qualified=len(matrix.qualified_vendor_ids),
        )
        return matrix

    # ----------------------------------------------------------- per supplier
    def _evaluate_supplier(
        self,
        case_id: str,
        quotation: Any,
        requirement_rows: list[Any],
        requirements: list[Requirement],
        *,
        use_model: bool,
    ) -> SupplierEvaluation:
        answers = self._collect_answers(case_id, quotation, requirement_rows, use_model=use_model)
        converter = UnitConverter()

        assessments: list[ComplianceAssessment] = []
        blockers: list[str] = []
        deviations: list[dict[str, Any]] = []
        unanswered: list[str] = []
        earned = Decimal(0)
        available = Decimal(0)
        mandatory_met = desirable_met = 0
        mandatory_total = desirable_total = 0

        for row, requirement in zip(requirement_rows, requirements, strict=True):
            answer = answers.get(row.requirement_key, {})
            offered_text = str(answer.get("offered_value", "") or "")
            offered_numeric, offered_uom = _numeric_from(offered_text, requirement)

            status, rationale = requirement.evaluate(
                offered_text=offered_text or None,
                offered_numeric=offered_numeric,
                offered_uom=offered_uom,
                converter=converter,
            )
            # An affirmative sentence against a numeric requirement is a claim,
            # not a measurement: treat it as unverifiable rather than compliant.
            if (
                status == ComplianceStatus.UNVERIFIABLE
                and _is_affirmative(offered_text)
                and requirement.operator
                not in (ComparisonOperator.BOOLEAN, ComparisonOperator.PRESENT)
            ):
                rationale = (
                    f"Supplier states compliance ({offered_text[:80]!r}) but did not provide the "
                    f"value required to verify {requirement.describe()}"
                )

            assessment = ComplianceAssessment(
                requirement_id=row.id,
                supplier_id=quotation.vendor_id,
                status=status,
                offered_value=offered_text[:2000],
                rationale=rationale,
                evidence_ids=tuple(answer.get("evidence_ids", ())),
                assessed_by=answer.get("source", "DETERMINISTIC"),
                confidence=Decimal(str(answer.get("confidence", 0))),
            )
            persisted = self.ctx.repos.compliance.upsert(
                case_id=case_id,
                quotation_id=quotation.id,
                requirement_id=row.id,
                vendor_id=quotation.vendor_id,
                status=str(status),
                offered_value=assessment.offered_value,
                rationale=rationale,
                offered_numeric=offered_numeric,
                offered_uom=offered_uom,
                evidence_ids=list(assessment.evidence_ids),
                confidence=assessment.confidence,
                assessed_by=assessment.assessed_by,
            )
            assessment.deviation_accepted = bool(persisted.deviation_accepted)
            assessment.deviation_approval_id = persisted.deviation_approval_id or ""
            assessments.append(assessment)

            weight = Decimal(str(row.weight or 1))
            is_mandatory = row.obligation == RequirementObligation.MANDATORY.value
            if is_mandatory:
                mandatory_total += 1
            elif row.obligation == RequirementObligation.DESIRABLE.value:
                desirable_total += 1

            if status == ComplianceStatus.COMPLIANT or assessment.deviation_accepted:
                available += weight
                earned += weight
                if is_mandatory:
                    mandatory_met += 1
                elif row.obligation == RequirementObligation.DESIRABLE.value:
                    desirable_met += 1
            elif status == ComplianceStatus.DEVIATION:
                available += weight
                # Partial credit: a declared deviation is far better than silence.
                earned += weight * Decimal("0.5")
                deviations.append(
                    {
                        "requirement_key": row.requirement_key,
                        "attribute": row.attribute,
                        "required": requirement.describe(),
                        "offered": offered_text[:200],
                        "obligation": row.obligation,
                        "rationale": rationale,
                    }
                )
                if is_mandatory:
                    blockers.append(f"{row.requirement_key} ({row.attribute}): {rationale}")
            else:
                available += weight
                if status == ComplianceStatus.NOT_ADDRESSED:
                    unanswered.append(f"{row.requirement_key}: {row.attribute}")
                if is_mandatory:
                    blockers.append(f"{row.requirement_key} ({row.attribute}): {rationale}")

        score = (earned / available * Decimal(100)).quantize(Decimal("0.01")) if available else Decimal(0)
        qualified = not blockers and mandatory_total >= 0

        self.ctx.repos.quotations.set_technical_result(
            quotation.id,
            qualified=qualified,
            score=score,
            disqualification_reasons=blockers,
        )
        return SupplierEvaluation(
            vendor_id=quotation.vendor_id,
            vendor_name=quotation.vendor_name,
            quotation_id=quotation.id,
            qualified=qualified,
            technical_score=score,
            mandatory_met=mandatory_met,
            mandatory_total=mandatory_total,
            desirable_met=desirable_met,
            desirable_total=desirable_total,
            blockers=blockers,
            deviations=deviations,
            unanswered=unanswered,
            assessments=assessments,
        )

    # -------------------------------------------------------------- extraction
    def _collect_answers(
        self, case_id: str, quotation: Any, requirement_rows: list[Any], *, use_model: bool
    ) -> dict[str, dict[str, Any]]:
        """Locate the supplier's answer to each requirement.

        Sources in decreasing directness: an explicit REQ-keyed answer, a line
        attribute, retrieval over the supplier's own document, then the model.
        """
        answers: dict[str, dict[str, Any]] = {}

        # 1. Explicit REQ-keyed answers, stored as claims at ingestion.
        for claim in self.ctx.repos.claims.find(case_id=case_id, predicate="offered_value", limit=1000):
            if not claim.subject.startswith(f"{quotation.vendor_id}:"):
                continue
            key = claim.subject.split(":", 1)[1]
            answers[key] = {
                "offered_value": claim.value_text,
                "source": "SUPPLIER_DECLARED",
                "confidence": float(claim.confidence or 0),
                "evidence_ids": [claim.id],
            }

        # 2. Structured technical attributes on quoted lines.
        for line in quotation.lines:
            for attribute, value in (line.technical_attributes or {}).items():
                key = str(attribute).upper()
                if key.startswith("REQ-") and key not in answers:
                    answers[key] = {
                        "offered_value": str(value),
                        "source": "QUOTATION_LINE",
                        "confidence": 0.8,
                        "evidence_ids": [line.id],
                    }

        # 3. Retrieval over the supplier's own quotation text.
        missing = [row for row in requirement_rows if row.requirement_key not in answers]
        if missing and quotation.document_version_id:
            chunks = self.ctx.repos.chunks.list_for_version(quotation.document_version_id)
            corpus = "\n".join(chunk.content for chunk in chunks)
            for row in list(missing):
                found = _find_in_text(corpus, row)
                if found:
                    answers[row.requirement_key] = {
                        "offered_value": found,
                        "source": "DOCUMENT_MATCH",
                        "confidence": 0.6,
                        "evidence_ids": [quotation.document_version_id],
                    }
                    missing.remove(row)

        # 4. Model supplement, extraction only.
        if use_model and missing:
            answers.update(self._model_extract(quotation, missing))
        return answers

    def _model_extract(self, quotation: Any, missing: list[Any]) -> dict[str, dict[str, Any]]:
        chunks = (
            self.ctx.repos.chunks.list_for_version(quotation.document_version_id)
            if quotation.document_version_id
            else []
        )
        corpus = "\n".join(chunk.content for chunk in chunks)[:24_000]
        if not corpus.strip():
            return {}

        wanted = "\n".join(
            f"{row.requirement_key}: {row.attribute}"
            + (f" (required: {row.target_value or row.target_numeric} {row.uom})" if row.uom or row.target_value else "")
            for row in missing[:60]
        )
        prompt = (
            trusted_block(wanted, label="REQUIREMENTS TO LOCATE")
            + "\n\n"
            + untrusted_block(corpus, label="SUPPLIER QUOTATION")
            + "\n\nFor each requirement, report the value the supplier offered, verbatim, with "
            "its location. If the supplier did not address it, set addressed=false. Do not "
            "judge compliance."
        )
        try:
            response = self.ctx.model.generate_json(
                system=COMPLIANCE_ASSESSMENT_SYSTEM,
                prompt=prompt,
                schema=COMPLIANCE_SCHEMA,
                purpose="compliance_extraction",
            )
        except Exception as exc:
            log.error("compliance_model_extraction_failed", detail=str(exc)[:300])
            return {}

        payload = response.content if isinstance(response.content, dict) else {}
        out: dict[str, dict[str, Any]] = {}
        for item in payload.get("answers", []) or []:
            if not isinstance(item, dict) or not item.get("addressed"):
                continue
            key = str(item.get("requirement_key", "")).upper()
            value = str(item.get("offered_value", "")).strip()
            if not key or not value:
                continue
            out[key] = {
                "offered_value": value,
                "source": "MODEL_EXTRACTED",
                # Capped: model-located values are always the weakest evidence
                # class, and a reviewer should be able to see that at a glance.
                "confidence": min(float(item.get("confidence", 0.5) or 0.5), 0.55),
                "evidence_ids": [quotation.document_version_id],
            }
        return out


# ------------------------------------------------------------------ helpers

def _to_domain(row: Any) -> Requirement:
    return Requirement(
        requirement_id=row.id,
        case_id=row.case_id,
        kind=RequirementKind(row.kind),
        obligation=RequirementObligation(row.obligation),
        attribute=row.attribute,
        operator=ComparisonOperator(row.operator),
        raw_text=row.raw_text or "",
        target_value=row.target_value or "",
        target_numeric=_dec(row.target_numeric),
        upper_numeric=_dec(row.upper_numeric),
        lower_numeric=_dec(row.lower_numeric),
        tolerance_plus=_dec(row.tolerance_plus),
        tolerance_minus=_dec(row.tolerance_minus),
        uom=row.uom or "",
        allowed_values=tuple(str(v) for v in (row.allowed_values or [])),
        weight=Decimal(str(row.weight or 1)),
        source_document_version_id=row.source_document_version_id or "",
        source_location=row.source_location or "",
        trust_state=TrustState(row.trust_state) if row.trust_state else TrustState.UNVERIFIED,
        extraction_confidence=Decimal(str(row.extraction_confidence or 0)),
    )


def _numeric_from(text: str, requirement: Requirement) -> tuple[Decimal | None, str]:
    """Pull a number and its unit out of a supplier's free-text answer."""
    if not text:
        return None, ""
    match = _NUMERIC.search(text)
    if not match:
        return None, ""
    try:
        value = Decimal(match.group("num").replace(",", "."))
    except Exception:
        return None, ""
    raw_uom = (match.group("uom") or "").strip()
    uom = normalize_engineering_unit(raw_uom) if raw_uom else requirement.uom
    return value, uom


def _is_affirmative(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    if any(token in lowered for token in _NEGATIVE):
        return False
    return any(token in lowered for token in _AFFIRMATIVE)


def _find_in_text(corpus: str, row: Any) -> str:
    """Locate a requirement's answer by matching its attribute in the text."""
    if not corpus:
        return ""
    tokens = [t for t in re.split(r"\W+", (row.attribute or "").lower()) if len(t) >= 4]
    if not tokens:
        return ""
    for line in corpus.splitlines():
        lowered = line.lower()
        hits = sum(1 for token in tokens if token in lowered)
        if hits >= max(1, len(tokens) // 2):
            # Return the value side of "attribute: value" where present.
            _, separator, tail = line.partition(":")
            candidate = (tail if separator else line).strip()
            if candidate and len(candidate) <= 300:
                return candidate
    return ""


def _dec(value: Any) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def _matrix_confidence(matrix: ComparisonMatrix) -> Decimal:
    if not matrix.evaluations:
        return Decimal(0)
    scores = [
        Decimal(str(cell.get("confidence", 0)))
        for by_vendor in matrix.cells.values()
        for cell in by_vendor.values()
    ]
    if not scores:
        return Decimal("0.3")
    return (sum(scores) / len(scores)).quantize(Decimal("0.0001"))
