"""Stage 1 - purchase requisition parsing.

Requisitions arrive in whatever shape the requester had to hand: a JSON payload
from a portal, a CSV export from SAP, a spreadsheet, or - most often in practice
- an email that says "we need 200 of these by the 15th".

The parser is deterministic and format-aware. It never guesses a material code
and never invents a unit: a quantity written without a unit is reported as
missing, because "200" of an unknown unit has caused more expensive mistakes in
procurement than almost anything else.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from procureguard.domain.entities import PurchaseRequisition, PurchaseRequisitionLine
from procureguard.domain.units import try_normalize_uom
from procureguard.observability import logger

log = logger(__name__)


@dataclass(slots=True)
class ParseResult:
    requisition: PurchaseRequisition
    confidence: Decimal
    warnings: list[str] = field(default_factory=list)
    source_format: str = "unknown"
    unparsed_remainder: str = ""

    def add_warning(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)


# Header aliases seen across SAP exports, Ariba/Coupa CSVs and hand-built sheets.
_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "line_number": ("line", "line_no", "line_number", "item", "item_no", "pos", "position", "seq"),
    "material_code": (
        "material", "material_code", "material_number", "matnr", "part", "part_no",
        "part_number", "item_code", "sku", "article", "stock_code",
    ),
    "description": (
        "description", "desc", "short_text", "text", "material_description", "item_description",
        "long_text", "specification",
    ),
    "quantity": ("quantity", "qty", "menge", "req_qty", "requested_quantity", "amount_qty"),
    "uom": ("uom", "unit", "units", "unit_of_measure", "meins", "uom_code", "u_m"),
    "required_date": (
        "required_date", "need_by", "need_by_date", "delivery_date", "requested_date",
        "due_date", "req_date", "eddat",
    ),
    "plant_code": ("plant", "plant_code", "werks", "site", "location"),
    "storage_location": ("storage_location", "sloc", "lgort"),
    "cost_center": ("cost_center", "cost_centre", "kostl", "costcenter"),
    "gl_account": ("gl_account", "gl", "account", "sakto"),
    "estimated_unit_price": (
        "price", "unit_price", "estimated_price", "est_price", "valuation_price", "preis",
    ),
    "currency": ("currency", "curr", "waers"),
    "specification_reference": (
        "spec", "spec_ref", "specification", "specification_reference", "drawing", "drawing_no",
    ),
    "manufacturer_part_number": ("mpn", "mfr_part", "manufacturer_part_number", "oem_part"),
    "preferred_vendor_id": ("vendor", "vendor_id", "supplier", "supplier_id", "preferred_vendor"),
    "notes": ("notes", "note", "remarks", "comment", "comments"),
}

_HEADER_LOOKUP = {
    alias: canonical for canonical, aliases in _HEADER_ALIASES.items() for alias in aliases
}

_HEADER_FIELDS: dict[str, tuple[str, ...]] = {
    "pr_number": ("pr number", "pr no", "requisition", "requisition number", "pr", "banfn", "req no"),
    "plant_code": ("plant", "site", "works", "location"),
    "requester": ("requester", "requested by", "raised by", "originator", "from"),
    "requester_email": ("email", "e-mail", "requester email"),
    "department": ("department", "dept", "cost center owner"),
    "priority": ("priority", "urgency"),
    "justification": ("justification", "reason", "business case", "purpose"),
    "budget_code": ("budget", "budget code", "wbs", "project"),
    "currency": ("currency",),
    "cost_center": ("cost center", "cost centre"),
    "required_date": (
        "required by", "required date", "need by", "needed by", "delivery date",
        "requested delivery", "due date", "deadline",
    ),
}

_DATE_FORMATS = (
    "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d",
    "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%Y%m%d",
)

_QTY_UOM = re.compile(
    r"(?P<qty>\d[\d,\s]*(?:\.\d+)?)\s*"
    r"(?P<uom>eaches|each|pcs|pieces?|nos|units?|kgs?|kilograms?|grams?|g\b|tonnes?|tons?|"
    r"metres?|meters?|mtrs?|m\b|mm|cm|litres?|liters?|ltr|l\b|boxes|box|rolls?|sets?|"
    r"pallets?|drums?|bags?|cartons?|packs?|ea\b|pc\b|kg\b)",
    re.IGNORECASE,
)

_MATERIAL_CODE = re.compile(r"\b(?:[A-Z]{2,5}-\d{3,6}(?:-[A-Z0-9]{1,4})?|\d{8,10})\b")
_PR_NUMBER = re.compile(r"\b(?:PR|REQ|BANF)[-_ ]?(\d{4,10})\b", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


class PurchaseRequisitionParser:
    """Deterministic multi-format requisition parser."""

    def parse(
        self,
        content: bytes | str,
        *,
        media_type: str = "",
        filename: str = "",
        source_channel: str = "API",
        default_plant: str = "",
        default_currency: str = "USD",
    ) -> ParseResult:
        text = content.decode("utf-8-sig", errors="replace") if isinstance(content, bytes) else content
        fmt = self._detect_format(text, media_type=media_type, filename=filename)

        match fmt:
            case "json":
                result = self._parse_json(text, source_channel, default_plant, default_currency)
            case "csv":
                result = self._parse_csv(text, source_channel, default_plant, default_currency)
            case _:
                result = self._parse_freetext(text, source_channel, default_plant, default_currency)
        result.source_format = fmt
        self._post_validate(result, default_currency)
        return result

    # ------------------------------------------------------------- detection
    @staticmethod
    def _detect_format(text: str, *, media_type: str, filename: str) -> str:
        lowered_type = (media_type or "").lower()
        suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if "json" in lowered_type or suffix == "json":
            return "json"
        if "csv" in lowered_type or suffix in ("csv", "tsv"):
            return "csv"

        stripped = text.strip()
        if stripped.startswith(("{", "[")):
            return "json"
        head = "\n".join(stripped.splitlines()[:3])
        # A real CSV header has several delimiter-separated known column names.
        for delimiter in (",", ";", "\t", "|"):
            if head.count(delimiter) >= 2:
                first = stripped.splitlines()[0]
                known = sum(
                    1
                    for cell in first.split(delimiter)
                    if _canonical_header(cell) is not None
                )
                if known >= 2:
                    return "csv"
        return "freetext"

    # ------------------------------------------------------------------ JSON
    def _parse_json(
        self, text: str, source_channel: str, default_plant: str, default_currency: str
    ) -> ParseResult:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            pr = PurchaseRequisition(pr_number="", plant_code=default_plant, requester="")
            return ParseResult(
                requisition=pr,
                confidence=Decimal(0),
                warnings=[f"JSON payload is malformed: {exc}"],
            )

        if isinstance(payload, list):
            payload = {"lines": payload}

        result = ParseResult(
            requisition=PurchaseRequisition(
                pr_number=str(payload.get("pr_number") or payload.get("prNumber") or "").strip(),
                plant_code=str(payload.get("plant_code") or payload.get("plant") or default_plant).strip(),
                requester=str(payload.get("requester") or payload.get("requested_by") or "").strip(),
                requester_email=str(payload.get("requester_email") or payload.get("email") or "").strip(),
                department=str(payload.get("department") or "").strip(),
                company_code=str(payload.get("company_code") or "").strip(),
                currency=str(payload.get("currency") or default_currency).strip().upper(),
                priority=str(payload.get("priority") or "NORMAL").strip().upper(),
                justification=str(payload.get("justification") or "").strip(),
                budget_code=str(payload.get("budget_code") or "").strip(),
                source_channel=source_channel,
            ),
            confidence=Decimal("0.98"),
        )

        raw_lines = payload.get("lines") or payload.get("items") or []
        for index, raw in enumerate(raw_lines, start=1):
            if not isinstance(raw, dict):
                result.add_warning(f"Line {index} is not an object and was skipped")
                continue
            normalized = {
                _HEADER_LOOKUP.get(str(key).strip().lower().replace(" ", "_"), str(key).strip().lower()): value
                for key, value in raw.items()
            }
            line = self._build_line(normalized, index, result, default_plant)
            if line:
                result.requisition.lines.append(line)
        return result

    # ------------------------------------------------------------------- CSV
    def _parse_csv(
        self, text: str, source_channel: str, default_plant: str, default_currency: str
    ) -> ParseResult:
        header_meta, table_text = self._split_preamble(text)
        try:
            dialect = csv.Sniffer().sniff(table_text[:4096], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(table_text), dialect=dialect)

        result = ParseResult(
            requisition=PurchaseRequisition(
                pr_number=header_meta.get("pr_number", ""),
                plant_code=header_meta.get("plant_code", default_plant),
                requester=header_meta.get("requester", ""),
                requester_email=header_meta.get("requester_email", ""),
                department=header_meta.get("department", ""),
                currency=(header_meta.get("currency") or default_currency).upper(),
                priority=(header_meta.get("priority") or "NORMAL").upper(),
                justification=header_meta.get("justification", ""),
                budget_code=header_meta.get("budget_code", ""),
                source_channel=source_channel,
            ),
            confidence=Decimal("0.9"),
        )

        unmapped = [
            name
            for name in (reader.fieldnames or [])
            if name and _canonical_header(name) is None
        ]
        if unmapped:
            result.add_warning(f"Unrecognised columns ignored: {sorted(unmapped)}")

        for index, raw in enumerate(reader, start=1):
            normalized: dict[str, Any] = {}
            for key, value in raw.items():
                canonical = _canonical_header(key or "")
                if canonical:
                    normalized[canonical] = value
            if not any(str(v or "").strip() for v in normalized.values()):
                continue
            line = self._build_line(normalized, index, result, default_plant)
            if line:
                result.requisition.lines.append(line)

        if not result.requisition.pr_number:
            match = _PR_NUMBER.search(text)
            if match:
                result.requisition.pr_number = match.group(0).upper().replace("_", "-")
        return result

    @staticmethod
    def _split_preamble(text: str) -> tuple[dict[str, str], str]:
        """Pull 'Key: value' header lines that precede a CSV table."""
        meta: dict[str, str] = {}
        lines = text.splitlines()
        table_start = 0
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                table_start = index + 1
                continue
            if ":" in stripped and stripped.count(",") <= 1:
                key, _, value = stripped.partition(":")
                canonical = _canonical_header_field(key)
                if canonical:
                    meta[canonical] = value.strip()
                    table_start = index + 1
                    continue
            break
        return meta, "\n".join(lines[table_start:])

    # -------------------------------------------------------------- freetext
    def _parse_freetext(
        self, text: str, source_channel: str, default_plant: str, default_currency: str
    ) -> ParseResult:
        """Extract a requisition from an email or memo.

        Confidence starts low and only rises with corroborating structure. The
        output is explicitly a *draft* that a buyer confirms; nothing here is
        trusted enough to raise an RFQ unattended.
        """
        body = _strip_quoted_reply(text)
        header = self._extract_header_fields(body)

        result = ParseResult(
            requisition=PurchaseRequisition(
                pr_number=header.get("pr_number", ""),
                plant_code=header.get("plant_code", default_plant),
                requester=header.get("requester", ""),
                requester_email=header.get("requester_email", ""),
                department=header.get("department", ""),
                currency=(header.get("currency") or default_currency).upper(),
                priority=(header.get("priority") or "NORMAL").upper(),
                justification=header.get("justification", ""),
                budget_code=header.get("budget_code", ""),
                source_channel=source_channel,
            ),
            confidence=Decimal("0.45"),
        )

        line_number = 0
        consumed: list[str] = []
        for raw_line in body.splitlines():
            stripped = raw_line.strip(" \t-*•·").strip()
            if len(stripped) < 4:
                continue
            match = _QTY_UOM.search(stripped)
            if not match:
                continue
            try:
                quantity = Decimal(match.group("qty").replace(",", "").replace(" ", ""))
            except InvalidOperation:
                continue
            if quantity <= 0:
                continue

            line_number += 1
            consumed.append(stripped)
            material = ""
            code_match = _MATERIAL_CODE.search(stripped)
            if code_match:
                material = code_match.group(0)

            # A date is only this line's date if it appears on this line. A date
            # mentioned elsewhere in the mail is a header-level default, applied
            # after the loop - otherwise one line's deadline silently becomes
            # every line's deadline.
            date_on_line, date_span = _extract_date_in(stripped)

            description = stripped
            for span in filter(None, [match.group(0), material, date_span]):
                description = description.replace(span, " ")
            description = re.sub(r"\b(?:of|for|by|the)\b\s*$", "", description.strip(), flags=re.I)
            description = re.sub(r"^\s*\b(?:of|for)\b\s+", "", description, flags=re.I)
            description = re.sub(r"\s{2,}", " ", description).strip(" ,;:-").strip()

            result.requisition.lines.append(
                PurchaseRequisitionLine(
                    line_number=line_number,
                    material_code=material,
                    quantity=quantity,
                    uom=try_normalize_uom(match.group("uom"), "EA"),
                    description=description or stripped,
                    required_date=date_on_line,
                    plant_code=result.requisition.plant_code,
                    free_text_only=not material,
                )
            )

        # Header-level "required by" applies only to lines that stated no date.
        header_date = _parse_date(header.get("required_date", "")) or _extract_date_in(
            _header_region(body)
        )[0]
        if header_date:
            for line in result.requisition.lines:
                if line.required_date is None:
                    line.required_date = header_date

        if not result.requisition.lines:
            result.add_warning(
                "No requisition lines could be identified; a buyer must complete the "
                "requisition manually before sourcing can start"
            )
            result.confidence = Decimal("0.1")
        else:
            if all(line.material_code for line in result.requisition.lines):
                result.confidence += Decimal("0.2")
            if result.requisition.pr_number:
                result.confidence += Decimal("0.1")
            if result.requisition.plant_code:
                result.confidence += Decimal("0.05")
            result.confidence = min(result.confidence, Decimal("0.85"))
            result.add_warning(
                "Requisition was extracted from unstructured text; confirm quantities, "
                "units and material codes before issuing an RFQ"
            )

        remainder_lines = [
            line for line in body.splitlines() if line.strip() and line.strip() not in consumed
        ]
        result.unparsed_remainder = "\n".join(remainder_lines)[:4000]
        return result

    @staticmethod
    def _extract_header_fields(text: str) -> dict[str, str]:
        found: dict[str, str] = {}
        for raw_line in text.splitlines():
            if ":" not in raw_line:
                continue
            key, _, value = raw_line.partition(":")
            canonical = _canonical_header_field(key)
            if canonical and value.strip():
                found.setdefault(canonical, value.strip())

        if "pr_number" not in found:
            match = _PR_NUMBER.search(text)
            if match:
                found["pr_number"] = match.group(0).upper().replace("_", "-").replace(" ", "-")
        if "requester_email" not in found:
            match = _EMAIL.search(text)
            if match:
                found["requester_email"] = match.group(0)
        if "priority" not in found and re.search(
            r"\b(urgent|asap|emergency|critical|line\s+down)\b", text, re.I
        ):
            found["priority"] = "URGENT"
        return found

    # --------------------------------------------------------------- helpers
    def _build_line(
        self,
        data: dict[str, Any],
        fallback_index: int,
        result: ParseResult,
        default_plant: str,
    ) -> PurchaseRequisitionLine | None:
        quantity = _to_decimal(data.get("quantity"))
        if quantity is None:
            result.add_warning(f"Line {fallback_index}: quantity is missing or unparseable")
            quantity = Decimal(0)

        raw_uom = str(data.get("uom") or "").strip()
        if not raw_uom:
            result.add_warning(
                f"Line {fallback_index}: no unit of measure supplied; it must be confirmed "
                f"before this line can be sourced"
            )
        uom = try_normalize_uom(raw_uom, "") if raw_uom else ""
        if raw_uom and not uom:
            result.add_warning(f"Line {fallback_index}: unrecognised unit {raw_uom!r}")
            uom = raw_uom.upper()

        material_code = str(data.get("material_code") or "").strip()
        description = str(data.get("description") or "").strip()

        return PurchaseRequisitionLine(
            line_number=int(_to_decimal(data.get("line_number")) or fallback_index),
            material_code=material_code,
            quantity=quantity,
            uom=uom or "EA" if not raw_uom else uom,
            description=description,
            required_date=_parse_date(data.get("required_date")),
            plant_code=str(data.get("plant_code") or default_plant or "").strip(),
            storage_location=str(data.get("storage_location") or "").strip(),
            cost_center=str(data.get("cost_center") or "").strip(),
            gl_account=str(data.get("gl_account") or "").strip(),
            estimated_unit_price=_to_decimal(data.get("estimated_unit_price")),
            currency=str(data.get("currency") or "").strip().upper(),
            specification_reference=str(data.get("specification_reference") or "").strip(),
            manufacturer_part_number=str(data.get("manufacturer_part_number") or "").strip(),
            preferred_vendor_id=str(data.get("preferred_vendor_id") or "").strip(),
            free_text_only=not material_code,
            notes=str(data.get("notes") or "").strip(),
        )

    @staticmethod
    def _post_validate(result: ParseResult, default_currency: str) -> None:
        pr = result.requisition
        if not pr.currency:
            pr.currency = default_currency
        for index, line in enumerate(pr.lines, start=1):
            if not line.line_number:
                line.line_number = index
            if not line.plant_code:
                line.plant_code = pr.plant_code
        seen: set[int] = set()
        for line in pr.lines:
            while line.line_number in seen:
                line.line_number += 1
            seen.add(line.line_number)

        for message in pr.validate():
            result.add_warning(message)
        if result.warnings and result.confidence > Decimal("0.5"):
            result.confidence -= Decimal("0.05") * min(len(result.warnings), 4)
        result.confidence = max(Decimal(0), min(result.confidence, Decimal(1)))


def _canonical_header(name: str) -> str | None:
    key = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return _HEADER_LOOKUP.get(key)


def _canonical_header_field(name: str) -> str | None:
    key = re.sub(r"\s+", " ", (name or "").strip().lower())
    for canonical, aliases in _HEADER_FIELDS.items():
        if key in aliases:
            return canonical
    return None


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip().replace(" ", "")
    if not text:
        return None
    # Strip currency symbols and thousands separators before conversion.
    text = re.sub(r"[^\d.,\-]", "", text)
    if text.count(",") and text.count("."):
        text = text.replace(",", "") if text.rfind(".") > text.rfind(",") else text.replace(".", "").replace(",", ".")
    elif text.count(","):
        tail = text.rsplit(",", 1)[1]
        text = text.replace(",", "") if len(tail) == 3 else text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _parse_date(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


_RELATIVE_DATE = re.compile(
    r"(?:within|in)\s+(?P<n>\d+)\s+(?P<unit>days?|weeks?|months?)"
    r"|by\s+(?P<date>\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{4}-\d{2}-\d{2})"
    r"|by\s+(?P<named>\d{1,2}(?:st|nd|rd|th)?\s+\w+(?:\s+\d{4})?)",
    re.IGNORECASE,
)


def _extract_date_in(scope: str) -> tuple[datetime | None, str]:
    """Find a required date inside one scope.

    Returns the date and the exact matched text, so the caller can strip that
    span out of the description instead of leaving "…, by 15/09/2026" in it.
    """
    match = _RELATIVE_DATE.search(scope or "")
    if not match:
        return None, ""
    span = match.group(0)
    if match.group("date"):
        parsed = _parse_date(match.group("date"))
        if parsed:
            return parsed, span
    if match.group("named"):
        cleaned = re.sub(r"(?<=\d)(st|nd|rd|th)", "", match.group("named"))
        if not re.search(r"\d{4}", cleaned):
            cleaned = f"{cleaned} {datetime.now(UTC).year}"
        parsed = _parse_date(cleaned)
        if parsed:
            return parsed, span
    if match.group("n"):
        count = int(match.group("n"))
        unit = match.group("unit").lower()
        days = count * (7 if unit.startswith("week") else 30 if unit.startswith("month") else 1)
        return datetime.now(UTC) + timedelta(days=days), span
    return None, ""


def _header_region(body: str) -> str:
    """Text before the first quantity-bearing line, i.e. the message preamble."""
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if _QTY_UOM.search(line):
            return "\n".join(lines[:index])
    return body


def _strip_quoted_reply(text: str) -> str:
    """Drop quoted history so a forwarded thread does not double-count lines."""
    markers = (
        "-----original message-----",
        "________________________________",
        "on wrote:",
        "from:",
    )
    lines = text.splitlines()
    kept: list[str] = []
    for line in lines:
        lowered = line.strip().lower()
        if lowered.startswith(">"):
            continue
        if any(lowered.startswith(marker) for marker in markers[:3]):
            break
        kept.append(line)
    return "\n".join(kept) if kept else text
