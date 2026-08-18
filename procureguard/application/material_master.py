"""Stage 2 - material-master validation.

This is the gate that stops a requisition from becoming an RFQ for something the
company cannot actually buy. Real reasons a line fails here, all of which this
implements:

* the material code does not exist, or exists only with different casing
* the material exists but is not extended to the requesting plant
* the material is blocked, obsolete, or superseded by a successor part
* it is made in-house, not purchased
* the requested unit is not convertible to the material's base unit
* the quantity violates minimum lot size or rounding value
* the required date is inside the planned delivery time

Free-text lines (no code) get a resolution attempt: exact manufacturer part
number, then token search, then vector similarity. A low-confidence match is
*proposed*, never applied - the buyer confirms it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from procureguard.domain.entities import PurchaseRequisition, PurchaseRequisitionLine
from procureguard.domain.enums import DecisionType, MaterialStatus, ProcurementType
from procureguard.domain.units import UnitConverter, normalize_uom
from procureguard.infrastructure.db.models import MaterialModel, MaterialPlantModel
from procureguard.infrastructure.factory import ServiceContext
from procureguard.observability import logger

log = logger(__name__)

# Confidence at or above which a fuzzy match is auto-applied. Below it the match
# becomes a suggestion requiring buyer confirmation.
AUTO_RESOLVE_THRESHOLD = Decimal("0.90")
SUGGEST_THRESHOLD = Decimal("0.55")


@dataclass(slots=True)
class MaterialResolution:
    line_number: int
    status: str  # VALID | RESOLVED | NEEDS_CONFIRMATION | BLOCKED | NOT_FOUND
    resolved_material_code: str = ""
    resolution_method: str = ""
    confidence: Decimal = Decimal(0)
    messages: list[str] = field(default_factory=list)
    blocking_messages: list[str] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    normalized_uom: str = ""
    base_uom: str = ""
    quantity_in_base_uom: Decimal | None = None
    material_group: str = ""
    material_description: str = ""
    requires_specification: bool = False
    planned_delivery_days: int = 0
    standard_price_base: Decimal | None = None

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocking_messages)

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_number": self.line_number,
            "status": self.status,
            "resolved_material_code": self.resolved_material_code,
            "resolution_method": self.resolution_method,
            "confidence": str(self.confidence),
            "messages": self.messages,
            "blocking_messages": self.blocking_messages,
            "candidates": self.candidates,
            "normalized_uom": self.normalized_uom,
            "base_uom": self.base_uom,
            "quantity_in_base_uom": (
                str(self.quantity_in_base_uom) if self.quantity_in_base_uom is not None else None
            ),
            "material_group": self.material_group,
            "material_description": self.material_description,
            "requires_specification": self.requires_specification,
            "planned_delivery_days": self.planned_delivery_days,
        }


@dataclass(slots=True)
class ValidationReport:
    pr_number: str
    resolutions: list[MaterialResolution] = field(default_factory=list)
    header_messages: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.blocking_messages

    @property
    def blocking_messages(self) -> list[str]:
        blocking = list(self.header_messages)
        for resolution in self.resolutions:
            blocking.extend(f"Line {resolution.line_number}: {m}" for m in resolution.blocking_messages)
        return blocking

    @property
    def needs_engineering(self) -> bool:
        """True when a line cannot be sourced without an engineering input."""
        return any(
            r.status in ("NEEDS_CONFIRMATION", "NOT_FOUND") or r.requires_specification
            for r in self.resolutions
        )

    def resolution_for(self, line_number: int) -> MaterialResolution | None:
        return next((r for r in self.resolutions if r.line_number == line_number), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pr_number": self.pr_number,
            "valid": self.is_valid,
            "needs_engineering": self.needs_engineering,
            "header_messages": self.header_messages,
            "lines": [r.to_dict() for r in self.resolutions],
        }


class MaterialMasterValidationService:
    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx
        self.materials = ctx.repos.materials
        self.vendors = ctx.repos.vendors

    def validate(self, pr: PurchaseRequisition, *, case_id: str = "") -> ValidationReport:
        report = ValidationReport(pr_number=pr.pr_number)

        plant = self.materials.get_plant(pr.plant_code)
        if plant is None:
            report.header_messages.append(
                f"Plant {pr.plant_code!r} does not exist in the plant master"
            )

        for line in pr.lines:
            report.resolutions.append(self._validate_line(pr, line))

        if case_id:
            self.ctx.repos.decisions.record(
                case_id=case_id,
                decision_type=DecisionType.MATERIAL_RESOLUTION.value,
                recommendation=report.to_dict(),
                rationale=(
                    f"{len([r for r in report.resolutions if r.status in ('VALID', 'RESOLVED')])} "
                    f"of {len(report.resolutions)} lines resolved against the material master"
                ),
                confidence=_mean_confidence(report),
                model_metadata={"engine": "deterministic-material-master"},
                evidence=[
                    {
                        "evidence_type": "MATERIAL_MASTER",
                        "evidence_id": r.resolved_material_code,
                        "role": "SOURCE_OF_TRUTH",
                        "excerpt": r.material_description,
                    }
                    for r in report.resolutions
                    if r.resolved_material_code
                ],
            )
        return report

    # -------------------------------------------------------------- per line
    def _validate_line(
        self, pr: PurchaseRequisition, line: PurchaseRequisitionLine
    ) -> MaterialResolution:
        resolution = MaterialResolution(line_number=line.line_number, status="NOT_FOUND")
        plant_code = line.plant_code or pr.plant_code

        material, method, confidence, candidates = self._resolve_material(line)
        resolution.candidates = candidates

        if material is None:
            resolution.status = "NOT_FOUND"
            resolution.requires_specification = True
            if candidates:
                resolution.messages.append(
                    f"No exact material match; {len(candidates)} similar materials proposed for "
                    f"buyer confirmation"
                )
            else:
                resolution.blocking_messages.append(
                    "No material could be identified. Provide a material code, a manufacturer "
                    "part number, or a technical specification for a free-text purchase."
                    if not line.description
                    else "No material master record matches this description; engineering must "
                    "either create a material or approve a free-text purchase with a specification."
                )
            self._validate_uom_without_master(line, resolution)
            return resolution

        resolution.resolved_material_code = material.material_code
        resolution.resolution_method = method
        resolution.confidence = confidence
        resolution.material_group = material.material_group
        resolution.material_description = material.description
        resolution.base_uom = material.base_uom

        if method == "EXACT":
            resolution.status = "VALID"
        elif confidence >= AUTO_RESOLVE_THRESHOLD:
            resolution.status = "RESOLVED"
            resolution.messages.append(
                f"Resolved to {material.material_code} ({material.description}) by {method} "
                f"at confidence {confidence}"
            )
        else:
            resolution.status = "NEEDS_CONFIRMATION"
            resolution.messages.append(
                f"Best match {material.material_code} ({material.description}) by {method} at "
                f"confidence {confidence}; buyer confirmation required before sourcing"
            )

        self._check_status(material, resolution)
        self._check_procurement_type(material, resolution)
        plant_extension = self._check_plant_extension(material, plant_code, resolution)
        self._check_uom(material, line, resolution)
        self._check_quantity(plant_extension, line, resolution)
        self._check_lead_time(plant_extension, line, resolution)
        self._check_specification(material, line, resolution)
        self._check_material_group(material, line, resolution)
        self._check_preferred_vendor(line, resolution, plant_code)

        if plant_extension is not None:
            resolution.planned_delivery_days = int(plant_extension.planned_delivery_days or 0)
            resolution.standard_price_base = (
                Decimal(str(plant_extension.standard_price))
                if plant_extension.standard_price is not None
                else None
            )
        if resolution.is_blocked:
            resolution.status = "BLOCKED"
        return resolution

    # -------------------------------------------------------------- resolution
    def _resolve_material(
        self, line: PurchaseRequisitionLine
    ) -> tuple[MaterialModel | None, str, Decimal, list[dict[str, Any]]]:
        code = (line.material_code or "").strip()

        if code:
            exact = self.materials.get(code)
            if exact:
                return exact, "EXACT", Decimal(1), []
            insensitive = self.materials.get_case_insensitive(code)
            if insensitive:
                return insensitive, "CASE_NORMALIZED", Decimal("0.99"), []

        if line.manufacturer_part_number:
            by_mpn = self.materials.find_by_manufacturer_part_number(line.manufacturer_part_number)
            if len(by_mpn) == 1:
                return by_mpn[0], "MANUFACTURER_PART_NUMBER", Decimal("0.95"), []
            if len(by_mpn) > 1:
                return (
                    None,
                    "MANUFACTURER_PART_NUMBER_AMBIGUOUS",
                    Decimal(0),
                    [_candidate(m, Decimal("0.7"), "MPN") for m in by_mpn[:5]],
                )

        search_text = " ".join(filter(None, [code, line.description])).strip()
        if not search_text:
            return None, "", Decimal(0), []

        candidates: dict[str, tuple[MaterialModel, Decimal, str]] = {}
        for material in self.materials.search_text(search_text, limit=8):
            candidates[material.material_code] = (
                material,
                _token_similarity(search_text, f"{material.material_code} {material.description}"),
                "TOKEN_SEARCH",
            )

        try:
            vector = self.ctx.embedder.embed(search_text)
            for material_code, score in self.materials.semantic_search(
                vector, top_k=8, dimensions=self.ctx.embedder.dimensions
            ):
                existing = candidates.get(material_code)
                similarity = Decimal(str(round(max(0.0, score), 4)))
                if existing is None:
                    material = self.materials.get(material_code)
                    if material:
                        candidates[material_code] = (material, similarity, "VECTOR_SIMILARITY")
                elif similarity > existing[1]:
                    # Agreement between lexical and vector search is a strong
                    # signal, so take the better score and say both matched.
                    candidates[material_code] = (existing[0], similarity, "TOKEN_AND_VECTOR")
        except Exception as exc:
            log.info("semantic_material_search_unavailable", detail=str(exc)[:200])

        if not candidates:
            return None, "", Decimal(0), []

        ranked = sorted(candidates.values(), key=lambda item: item[1], reverse=True)
        best_material, best_score, best_method = ranked[0]
        candidate_dicts = [_candidate(m, s, method) for m, s, method in ranked[:5]]

        if best_score >= AUTO_RESOLVE_THRESHOLD:
            return best_material, best_method, best_score, candidate_dicts
        if best_score >= SUGGEST_THRESHOLD:
            return best_material, best_method, best_score, candidate_dicts
        return None, best_method, best_score, candidate_dicts

    # ------------------------------------------------------------------ checks
    @staticmethod
    def _check_status(material: MaterialModel, resolution: MaterialResolution) -> None:
        try:
            status = MaterialStatus(material.status)
        except ValueError:
            resolution.messages.append(f"Unknown material status {material.status!r}")
            return
        if not status.blocks_procurement:
            return
        if status == MaterialStatus.OBSOLETE and material.successor_material_code:
            resolution.blocking_messages.append(
                f"Material {material.material_code} is OBSOLETE; successor "
                f"{material.successor_material_code} should be requisitioned instead"
            )
        elif status == MaterialStatus.OBSOLETE:
            resolution.blocking_messages.append(
                f"Material {material.material_code} is OBSOLETE with no successor recorded; "
                f"engineering must nominate a replacement"
            )
        elif status == MaterialStatus.ENGINEERING_HOLD:
            resolution.blocking_messages.append(
                f"Material {material.material_code} is on ENGINEERING HOLD and cannot be ordered"
            )
        elif status == MaterialStatus.PHASE_OUT:
            resolution.messages.append(
                f"Material {material.material_code} is in PHASE-OUT; confirm this is a "
                f"last-time buy before issuing the RFQ"
            )
        else:
            resolution.blocking_messages.append(
                f"Material {material.material_code} is {status} and blocked for procurement"
            )

    @staticmethod
    def _check_procurement_type(material: MaterialModel, resolution: MaterialResolution) -> None:
        if material.procurement_type == ProcurementType.INTERNAL.value:
            resolution.blocking_messages.append(
                f"Material {material.material_code} is manufactured in-house "
                f"(procurement type INTERNAL) and must not be bought externally"
            )

    def _check_plant_extension(
        self, material: MaterialModel, plant_code: str, resolution: MaterialResolution
    ) -> MaterialPlantModel | None:
        if not plant_code:
            resolution.blocking_messages.append("No plant specified for this line")
            return None
        extension = self.materials.get_plant_extension(material.material_code, plant_code)
        if extension is None:
            available = self.materials.list_plants_for_material(material.material_code)
            resolution.blocking_messages.append(
                f"Material {material.material_code} is not extended to plant {plant_code}. "
                + (
                    f"It exists at plants {available}; extend the material master or requisition "
                    f"from a plant where it is active."
                    if available
                    else "It is not extended to any plant."
                )
            )
            return None
        if extension.status != MaterialStatus.ACTIVE.value:
            resolution.blocking_messages.append(
                f"Material {material.material_code} is {extension.status} at plant {plant_code}"
            )
        return extension

    def _check_uom(
        self, material: MaterialModel, line: PurchaseRequisitionLine, resolution: MaterialResolution
    ) -> None:
        raw = (line.uom or "").strip()
        if not raw:
            resolution.blocking_messages.append("Unit of measure is missing")
            return
        try:
            requested = normalize_uom(raw)
        except Exception:
            resolution.blocking_messages.append(f"Unit of measure {raw!r} is not recognised")
            return
        resolution.normalized_uom = requested

        base = material.base_uom or "EA"
        converter = UnitConverter(self.materials.get_alternate_units(material.material_code))
        if not converter.can_convert(requested, base):
            resolution.blocking_messages.append(
                f"Requested unit {requested} cannot be converted to the material's base unit "
                f"{base}; a material-master alternate unit is required"
            )
            return
        resolution.quantity_in_base_uom = converter.convert(line.quantity, requested, base)
        if requested != base:
            resolution.messages.append(
                f"{line.quantity} {requested} converts to {resolution.quantity_in_base_uom} {base}"
            )

    def _validate_uom_without_master(
        self, line: PurchaseRequisitionLine, resolution: MaterialResolution
    ) -> None:
        """Validate the unit on a line that matched no material master record.

        There is no base unit to convert into, so the requested unit is its own
        base. It must still be recognised: an unparseable unit on a free-text line
        would otherwise reach the RFQ unchallenged and be quoted against by
        guesswork, which is exactly the ambiguity the unit table exists to refuse.
        """
        raw = (line.uom or "").strip()
        if not raw:
            resolution.blocking_messages.append("Unit of measure is missing")
            return
        try:
            requested = normalize_uom(raw)
        except Exception:
            resolution.blocking_messages.append(f"Unit of measure {raw!r} is not recognised")
            return
        resolution.normalized_uom = requested
        resolution.quantity_in_base_uom = line.quantity

    @staticmethod
    def _check_quantity(
        extension: MaterialPlantModel | None,
        line: PurchaseRequisitionLine,
        resolution: MaterialResolution,
    ) -> None:
        if extension is None or resolution.quantity_in_base_uom is None:
            return
        quantity = resolution.quantity_in_base_uom
        minimum = Decimal(str(extension.minimum_lot_size or 0))
        if minimum > 0 and quantity < minimum:
            resolution.messages.append(
                f"Requested {quantity} {resolution.base_uom} is below the minimum lot size "
                f"{minimum}; the RFQ will request {minimum}"
            )
        rounding = Decimal(str(extension.rounding_value or 0))
        if rounding > 1 and quantity % rounding != 0:
            rounded = (quantity / rounding).to_integral_value(rounding="ROUND_CEILING") * rounding
            resolution.messages.append(
                f"Quantity {quantity} is not a multiple of the rounding value {rounding}; "
                f"round up to {rounded}"
            )

    @staticmethod
    def _check_lead_time(
        extension: MaterialPlantModel | None,
        line: PurchaseRequisitionLine,
        resolution: MaterialResolution,
    ) -> None:
        if extension is None or line.required_date is None:
            return
        planned = int(extension.planned_delivery_days or 0) + int(
            extension.goods_receipt_processing_days or 0
        )
        earliest = datetime.now(UTC) + timedelta(days=planned)
        if line.required_date < earliest:
            shortfall = (earliest - line.required_date).days
            resolution.messages.append(
                f"Required date {line.required_date.date()} is {shortfall} day(s) inside the "
                f"planned delivery time of {planned} days; expedite or renegotiate the date"
            )

    @staticmethod
    def _check_specification(
        material: MaterialModel, line: PurchaseRequisitionLine, resolution: MaterialResolution
    ) -> None:
        has_spec = bool(
            line.specification_reference
            or material.specification_reference
            or material.drawing_number
        )
        if material.quality_inspection_required and not has_spec:
            resolution.requires_specification = True
            resolution.messages.append(
                f"Material {material.material_code} is quality-inspection relevant but no "
                f"specification or drawing is referenced; engineering input is required"
            )
        if not has_spec and not material.long_description:
            resolution.requires_specification = True
            resolution.messages.append(
                "No specification, drawing or long description is available to put in the RFQ"
            )

    @staticmethod
    def _check_material_group(
        material: MaterialModel, line: PurchaseRequisitionLine, resolution: MaterialResolution
    ) -> None:
        """Compare the requester's material group with the master.

        Not blocking: the master is authoritative and sourcing proceeds on it. But
        a mismatch is worth surfacing, because the material group drives the
        purchasing group, the approval limits and which suppliers are on the source
        list — so a wrong group in the requisition usually means the requester had
        a different part in mind than the code they typed.
        """
        requested = (line.requested_material_group or "").strip().upper()
        if not requested or not material.material_group:
            return
        if requested != material.material_group.strip().upper():
            resolution.messages.append(
                f"Requisition states material group {requested} but the master records "
                f"{material.material_group} for {material.material_code}; sourcing follows the "
                f"master. Confirm the requested part is correct."
            )

    def _check_preferred_vendor(
        self, line: PurchaseRequisitionLine, resolution: MaterialResolution, plant_code: str
    ) -> None:
        if not line.preferred_vendor_id:
            return
        vendor = self.vendors.get(line.preferred_vendor_id)
        if vendor is None:
            resolution.messages.append(
                f"Preferred vendor {line.preferred_vendor_id} does not exist in the vendor master"
            )
            return
        if vendor.status != "ACTIVE":
            resolution.messages.append(
                f"Preferred vendor {vendor.vendor_id} is {vendor.status} and cannot be invited"
            )
        elif resolution.resolved_material_code:
            approved = self.vendors.approved_for_material(
                resolution.resolved_material_code, plant_code
            )
            if approved and vendor.vendor_id not in approved:
                resolution.messages.append(
                    f"Preferred vendor {vendor.vendor_id} is not on the source list for "
                    f"{resolution.resolved_material_code}; a single-source justification will "
                    f"be required to award to them"
                )


def _candidate(material: MaterialModel, score: Decimal, method: str) -> dict[str, Any]:
    return {
        "material_code": material.material_code,
        "description": material.description,
        "material_group": material.material_group,
        "base_uom": material.base_uom,
        "status": material.status,
        "score": str(score),
        "method": method,
    }


def _token_similarity(a: str, b: str) -> Decimal:
    """Weighted Jaccard over tokens, favouring rarer long tokens."""
    tokens_a = _tokens(a)
    tokens_b = _tokens(b)
    if not tokens_a or not tokens_b:
        return Decimal(0)
    intersection = tokens_a & tokens_b
    if not intersection:
        return Decimal(0)
    weight = lambda tokens: sum(len(t) for t in tokens)  # noqa: E731
    score = (2 * weight(intersection)) / (weight(tokens_a) + weight(tokens_b))
    return Decimal(str(round(min(score, 1.0), 4)))


def _tokens(text: str) -> set[str]:
    cleaned = "".join(c if c.isalnum() else " " for c in (text or "").lower())
    return {t for t in cleaned.split() if len(t) >= 2}


def _mean_confidence(report: ValidationReport) -> Decimal:
    if not report.resolutions:
        return Decimal(0)
    total = sum(r.confidence for r in report.resolutions)
    return (total / len(report.resolutions)).quantize(Decimal("0.0001"))
