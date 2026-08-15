"""Stage 9 - quotation parsing.

Suppliers answer an RFQ in whatever form suits them: the CSV template we sent,
a PDF with a price table, a spreadsheet, or three lines in the body of an email.
This parser handles all four and reports what it could not determine, because a
silently-missing freight charge is how a bid comparison ends up wrong.

Nothing is normalised here. "per 100 pcs" is recorded as a price of X per 100,
"EXW Shenzhen" is recorded verbatim. Conversion to a comparable basis happens in
stage 12, against audited rate tables.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from procureguard.domain.enums import Incoterm
from procureguard.domain.money import CURRENCY_EXPONENT, CURRENCY_SYMBOLS
from procureguard.domain.units import try_normalize_uom
from procureguard.observability import logger

log = logger(__name__)

NUM = r"\d[\d,\s]*(?:[.,]\d+)?"
_CURRENCY_TOKENS = "|".join(sorted(CURRENCY_EXPONENT, key=len, reverse=True))


@dataclass(slots=True)
class ParsedQuotationLine:
    rfq_line_number: int
    material_code: str = ""
    offered_description: str = ""
    offered_part_number: str = ""
    quantity: Decimal | None = None
    uom: str = ""
    unit_price: Decimal | None = None
    price_per_quantity: Decimal = Decimal(1)
    currency: str = ""
    lead_time_days: int = 0
    minimum_order_quantity: Decimal | None = None
    line_total: Decimal | None = None
    notes: str = ""
    is_alternative: bool = False
    quantity_breaks: list[dict[str, Any]] = field(default_factory=list)
    technical_attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def is_priced(self) -> bool:
        return self.unit_price is not None and self.unit_price > 0


@dataclass(slots=True)
class ParsedQuotation:
    quotation_number: str = ""
    currency: str = ""
    incoterm: str = ""
    incoterm_location: str = ""
    payment_terms: str = ""
    validity_days: int = 0
    valid_until: datetime | None = None
    lead_time_days: int = 0
    freight_amount: Decimal = Decimal(0)
    packing_amount: Decimal = Decimal(0)
    tooling_amount: Decimal = Decimal(0)
    other_charges: Decimal = Decimal(0)
    discount_amount: Decimal = Decimal(0)
    total_amount: Decimal | None = None
    warranty_months: int = 0
    minimum_order_quantity: Decimal | None = None
    declined: bool = False
    decline_reason: str = ""
    lines: list[ParsedQuotationLine] = field(default_factory=list)
    technical_answers: dict[str, str] = field(default_factory=dict)
    confidence: Decimal = Decimal("0.5")
    warnings: list[str] = field(default_factory=list)
    source_format: str = "freetext"

    def add_warning(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    @property
    def priced_lines(self) -> list[ParsedQuotationLine]:
        return [line for line in self.lines if line.is_priced]

    def to_dict(self) -> dict[str, Any]:
        return {
            "quotation_number": self.quotation_number,
            "currency": self.currency,
            "incoterm": self.incoterm,
            "incoterm_location": self.incoterm_location,
            "payment_terms": self.payment_terms,
            "validity_days": self.validity_days,
            "lead_time_days": self.lead_time_days,
            "freight_amount": str(self.freight_amount),
            "packing_amount": str(self.packing_amount),
            "tooling_amount": str(self.tooling_amount),
            "other_charges": str(self.other_charges),
            "discount_amount": str(self.discount_amount),
            "total_amount": str(self.total_amount) if self.total_amount is not None else None,
            "warranty_months": self.warranty_months,
            "declined": self.declined,
            "confidence": str(self.confidence),
            "source_format": self.source_format,
            "warnings": self.warnings,
            "line_count": len(self.lines),
            "priced_line_count": len(self.priced_lines),
        }


class QuotationParser:
    def parse(
        self,
        text: str,
        *,
        rfq_lines: list[dict[str, Any]] | None = None,
        default_currency: str = "USD",
        filename: str = "",
    ) -> ParsedQuotation:
        quotation = ParsedQuotation()
        content = text or ""

        if _is_decline(content):
            quotation.declined = True
            quotation.decline_reason = content.strip()[:2000]
            quotation.confidence = Decimal("0.9")
            quotation.source_format = "decline"
            return quotation

        self._parse_header(content, quotation, default_currency)

        if self._looks_like_template_csv(content):
            quotation.source_format = "csv_template"
            self._parse_template_csv(content, quotation)
        else:
            rows = self._parse_table(content, quotation, rfq_lines or [])
            if rows:
                quotation.source_format = "table"
            else:
                self._parse_freetext_lines(content, quotation, rfq_lines or [])
                quotation.source_format = "freetext"

        self._parse_technical_answers(content, quotation)
        self._finalise(quotation, rfq_lines or [], default_currency)
        return quotation

    # ------------------------------------------------------------------ header
    def _parse_header(self, text: str, quotation: ParsedQuotation, default_currency: str) -> None:
        quotation.quotation_number = _first(
            text,
            r"(?:quotation|quote|offer|proposal)\s*(?:no\.?|number|ref\.?|reference)?\s*[:#]?\s*"
            r"([A-Z0-9][A-Z0-9\-/_]{2,24})",
        )
        quotation.currency = self._detect_currency(text) or ""

        incoterm_match = re.search(
            rf"\b(?P<term>{'|'.join(t.value for t in Incoterm)})\b[\s,]*(?P<place>[A-Z][\w .'-]{{2,40}})?",
            text,
        )
        if incoterm_match:
            quotation.incoterm = incoterm_match.group("term").upper()
            place = (incoterm_match.group("place") or "").strip(" .,;:")
            # Reject trailing prose that is not a place name.
            if place and not re.match(
                r"(?i)^(?:terms?|price|delivery|incoterms?|our|the|and|with|per|is|are)\b", place
            ):
                quotation.incoterm_location = place[:120]

        payment = _first(
            text,
            r"(?:payment\s*terms?|terms\s*of\s*payment|payment)\s*[:\-]?\s*([^\n\r]{3,80})",
        )
        if payment:
            quotation.payment_terms = payment.strip(" .;,")[:120]

        validity = _first(
            text, rf"(?:validity|valid\s*(?:for|until|till)?)\s*[:\-]?\s*({NUM})\s*(?:days?|d\b)"
        )
        if validity:
            quotation.validity_days = int(_dec(validity) or 0)
        else:
            until = _first(
                text,
                r"valid\s*(?:until|till|through)\s*[:\-]?\s*"
                r"(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}\s+\w+\s+\d{4})",
            )
            parsed_until = _parse_date(until) if until else None
            if parsed_until:
                quotation.valid_until = parsed_until
                quotation.validity_days = max(0, (parsed_until - datetime.now(UTC)).days)

        lead = re.search(
            rf"(?:lead\s*time|delivery\s*(?:time|period|lead)?|dispatch|ready\s*for\s*shipment)"
            rf"[^\n\r]{{0,30}}?({NUM})\s*(?P<unit>working\s+days?|business\s+days?|days?|weeks?|months?)",
            text,
            re.IGNORECASE,
        )
        if lead:
            value = _dec(lead.group(1)) or Decimal(0)
            unit = lead.group("unit").lower()
            if "week" in unit:
                days = value * 7
            elif "month" in unit:
                days = value * 30
            elif "working" in unit or "business" in unit:
                # 5-day weeks: 20 working days is 28 calendar days, and quoting
                # the calendar figure is what the delivery date depends on.
                days = value * 7 / 5
            else:
                days = value
            quotation.lead_time_days = int(days)

        warranty = re.search(
            rf"(?:warranty|guarantee)[^\n\r]{{0,25}}?({NUM})\s*(?P<unit>months?|years?|hours?)",
            text,
            re.IGNORECASE,
        )
        if warranty:
            value = _dec(warranty.group(1)) or Decimal(0)
            quotation.warranty_months = int(value * 12 if "year" in warranty.group("unit").lower() else value)

        quotation.freight_amount = _amount(text, r"freight|carriage|shipping|transport") or Decimal(0)
        quotation.packing_amount = _amount(text, r"packing|packaging|crating") or Decimal(0)
        quotation.tooling_amount = _amount(text, r"tooling|mould|mold|die|setup|set-up|nre") or Decimal(0)
        quotation.other_charges = _amount(text, r"other charges|handling|documentation fee|surcharge") or Decimal(0)
        quotation.discount_amount = _amount(text, r"discount|rebate|allowance") or Decimal(0)

        total = _amount(text, r"(?:grand\s*)?total(?:\s*(?:amount|price|value))?|net\s*total|sum")
        if total is not None:
            quotation.total_amount = total

        moq = _first(text, rf"(?:minimum\s*order\s*(?:quantity|qty)|moq)\s*[:\-]?\s*({NUM})")
        if moq:
            quotation.minimum_order_quantity = _dec(moq)

    @staticmethod
    def _detect_currency(text: str) -> str | None:
        iso = re.search(rf"\b({_CURRENCY_TOKENS})\b", text.upper())
        if iso:
            return iso.group(1)
        for symbol, code in sorted(CURRENCY_SYMBOLS.items(), key=lambda kv: -len(kv[0])):
            if symbol in text:
                return code
        return None

    # -------------------------------------------------------------------- CSV
    @staticmethod
    def _looks_like_template_csv(text: str) -> bool:
        head = text.strip().splitlines()[0].lower() if text.strip() else ""
        return "line_number" in head and "unit_price" in head

    def _parse_template_csv(self, text: str, quotation: ParsedQuotation) -> None:
        reader = csv.DictReader(io.StringIO(text.strip()))
        for row in reader:
            price = _dec(row.get("unit_price"))
            if price is None:
                continue
            quotation.lines.append(
                ParsedQuotationLine(
                    rfq_line_number=int(_dec(row.get("line_number")) or 0),
                    material_code=(row.get("material_code") or "").strip(),
                    offered_description=(row.get("description") or "").strip(),
                    quantity=_dec(row.get("quantity")),
                    uom=try_normalize_uom(row.get("uom") or "", "EA"),
                    unit_price=price,
                    price_per_quantity=_dec(row.get("price_per_quantity")) or Decimal(1),
                    currency=(row.get("currency") or quotation.currency or "").upper(),
                    lead_time_days=int(_dec(row.get("lead_time_days")) or 0),
                    minimum_order_quantity=_dec(row.get("minimum_order_quantity")),
                    notes=(row.get("notes") or "").strip(),
                )
            )
        quotation.confidence = Decimal("0.95")

    # ------------------------------------------------------------------ table
    def _parse_table(
        self, text: str, quotation: ParsedQuotation, rfq_lines: list[dict[str, Any]]
    ) -> bool:
        """Parse tab- or pipe-delimited price tables (PDF and XLSX extracts)."""
        rows: list[list[str]] = []
        for raw in text.splitlines():
            if "\t" in raw:
                cells = [c.strip() for c in raw.split("\t")]
            elif raw.count("|") >= 2:
                cells = [c.strip() for c in raw.strip("|").split("|")]
            else:
                continue
            if len([c for c in cells if c]) >= 3:
                rows.append(cells)
        if len(rows) < 2:
            return False

        header_index = _find_header_row(rows)
        if header_index is None:
            return False
        header = [c.lower() for c in rows[header_index]]
        columns = _map_columns(header)
        if "unit_price" not in columns:
            return False

        found = False
        for cells in rows[header_index + 1 :]:
            if len(cells) < len(header) - 2:
                continue
            price = _dec(_cell(cells, columns.get("unit_price")))
            if price is None or price <= 0:
                continue
            line_number = int(_dec(_cell(cells, columns.get("line_number"))) or 0)
            material = _cell(cells, columns.get("material_code"))
            if not line_number:
                line_number = _infer_line_number(material, _cell(cells, columns.get("description")), rfq_lines)
            quotation.lines.append(
                ParsedQuotationLine(
                    rfq_line_number=line_number,
                    material_code=material,
                    offered_description=_cell(cells, columns.get("description")),
                    offered_part_number=_cell(cells, columns.get("part_number")),
                    quantity=_dec(_cell(cells, columns.get("quantity"))),
                    uom=try_normalize_uom(_cell(cells, columns.get("uom")) or "", "EA"),
                    unit_price=price,
                    price_per_quantity=_dec(_cell(cells, columns.get("price_per"))) or Decimal(1),
                    currency=(_cell(cells, columns.get("currency")) or quotation.currency).upper(),
                    lead_time_days=int(_dec(_cell(cells, columns.get("lead_time"))) or 0),
                    line_total=_dec(_cell(cells, columns.get("line_total"))),
                    notes=_cell(cells, columns.get("notes")),
                )
            )
            found = True
        if found:
            quotation.confidence = Decimal("0.85")
        return found

    # --------------------------------------------------------------- freetext
    _LINE_PATTERNS = (
        # "Item 10: 250 pcs at USD 142.50 each"
        re.compile(
            rf"(?:item|line|pos(?:ition)?)\s*#?\s*(?P<line>\d{{1,4}})\s*[:.\-]?\s*"
            rf"(?P<body>.*?)(?P<qty>{NUM})\s*(?P<uom>[A-Za-z]{{1,6}})?\s*"
            rf"(?:@|at|for|x)\s*(?P<cur>[A-Z]{{3}}|[$€£¥₹])?\s*(?P<price>{NUM})",
            re.IGNORECASE,
        ),
        # "VAL-1023  250 EA  USD 142.50"
        re.compile(
            rf"(?P<code>[A-Z]{{2,5}}-\d{{3,6}}(?:-[A-Z0-9]{{1,4}})?)\s+"
            rf"(?P<qty>{NUM})\s*(?P<uom>[A-Za-z]{{1,6}})?\s+"
            rf"(?P<cur>[A-Z]{{3}}|[$€£¥₹])?\s*(?P<price>{NUM})",
            re.IGNORECASE,
        ),
        # "Unit price: USD 142.50" for a single-line enquiry
        re.compile(
            rf"unit\s*price\s*[:\-]?\s*(?P<cur>[A-Z]{{3}}|[$€£¥₹])?\s*(?P<price>{NUM})",
            re.IGNORECASE,
        ),
    )

    # "per 100 pcs", "/100 EA", "per box of 25". Losing this multiplies a bid by
    # a hundred, so it is captured explicitly rather than assumed to be 1.
    _PRICE_BASIS = re.compile(
        rf"(?:per|/|for)\s*(?P<per>{NUM})\s*(?P<per_uom>[A-Za-z]{{1,6}})?",
        re.IGNORECASE,
    )
    # Bare "ea" is deliberately absent: it is indistinguishable from the EA unit
    # of measure, and "per 100 EA" must not be read as "per each".
    _PER_EACH = re.compile(r"\b(?:each|per\s+(?:piece|pc|unit|item))\b", re.IGNORECASE)

    def _parse_freetext_lines(
        self, text: str, quotation: ParsedQuotation, rfq_lines: list[dict[str, Any]]
    ) -> None:
        """Match line items one source line at a time.

        Line-oriented rather than a scan over the whole body, because the price
        basis appears *after* the price on the same line and has to be read from
        that line's tail - not from wherever the next "per" happens to be.
        """
        seen: set[int] = set()
        for pattern in self._LINE_PATTERNS:
            for raw_line in text.splitlines():
                match = pattern.search(raw_line)
                if not match:
                    continue
                groups = match.groupdict()
                price = _dec(groups.get("price"))
                if price is None or price <= 0:
                    continue

                line_number = int(_dec(groups.get("line")) or 0)
                code = (groups.get("code") or "").strip()
                if not line_number:
                    line_number = _infer_line_number(code, groups.get("body") or "", rfq_lines)
                if not line_number:
                    line_number = (max(seen) + 1) if seen else 1
                if line_number in seen:
                    continue
                seen.add(line_number)

                # An explicit "per N" wins over an "each"; only fall back to a
                # per-unit reading when no quantity basis is stated at all.
                per = Decimal(1)
                tail = raw_line[match.end() :]
                basis = self._PRICE_BASIS.search(tail)
                if basis:
                    candidate = _dec(basis.group("per"))
                    if candidate and candidate > 0:
                        per = candidate

                currency = (groups.get("cur") or "").strip()
                quotation.lines.append(
                    ParsedQuotationLine(
                        rfq_line_number=line_number,
                        material_code=code,
                        offered_description=(groups.get("body") or "").strip(" .,:-")[:500],
                        quantity=_dec(groups.get("qty")),
                        uom=try_normalize_uom(groups.get("uom") or "", "EA"),
                        unit_price=price,
                        price_per_quantity=per,
                        currency=(
                            CURRENCY_SYMBOLS.get(currency, currency).upper()
                            if currency
                            else quotation.currency
                        ),
                    )
                )
            if quotation.lines:
                break
        if quotation.lines:
            quotation.confidence = Decimal("0.6")
            quotation.add_warning(
                "Line prices were extracted from unstructured text; verify each line against "
                "the supplier's original document before award"
            )

    # Horizontal whitespace only around the separator: \s* would swallow the
    # newline after an unanswered "REQ-009:" and capture the *next* line's
    # answer, silently attributing one requirement's value to another.
    _ANSWER = re.compile(
        r"^[ \t]*(?P<ref>REQ-\d{3})[ \t]*[:\-|\t][ \t]*(?P<answer>[^\r\n]{1,200})$",
        re.MULTILINE | re.IGNORECASE,
    )

    def _parse_technical_answers(self, text: str, quotation: ParsedQuotation) -> None:
        for match in self._ANSWER.finditer(text):
            answer = match.group("answer").strip(" |\t")
            if answer:
                quotation.technical_answers[match.group("ref").upper()] = answer[:500]

    # --------------------------------------------------------------- finalise
    def _finalise(
        self, quotation: ParsedQuotation, rfq_lines: list[dict[str, Any]], default_currency: str
    ) -> None:
        if not quotation.currency:
            line_currency = next((line.currency for line in quotation.lines if line.currency), "")
            quotation.currency = line_currency or default_currency
            if not line_currency:
                quotation.add_warning(
                    f"No currency was stated; assuming {default_currency}. Confirm with the "
                    f"supplier before comparing this bid."
                )
        for line in quotation.lines:
            if not line.currency:
                line.currency = quotation.currency
            if line.quantity is None and rfq_lines:
                match = next(
                    (r for r in rfq_lines if int(r.get("line_number", 0)) == line.rfq_line_number),
                    None,
                )
                if match:
                    line.quantity = Decimal(str(match.get("quantity", 0)))
                    line.uom = line.uom or str(match.get("uom", "EA"))
            if line.line_total is None and line.quantity is not None and line.unit_price is not None:
                line.line_total = (
                    line.unit_price / (line.price_per_quantity or Decimal(1))
                ) * line.quantity

        if not quotation.lines and not quotation.declined:
            quotation.add_warning(
                "No priced line items could be extracted. The quotation must be entered "
                "manually or the supplier asked to resubmit using the response template."
            )
            quotation.confidence = Decimal("0.1")

        if not quotation.incoterm:
            quotation.add_warning(
                "No Incoterm stated; the delivery-cost basis is unknown and this bid is not "
                "yet comparable"
            )
        if not quotation.payment_terms:
            quotation.add_warning("No payment terms stated")
        if not quotation.validity_days:
            quotation.add_warning("No quotation validity period stated")
        if not quotation.lead_time_days:
            quotation.add_warning("No lead time stated")

        # Cross-check the supplier's own arithmetic. A mismatch is usually a
        # missing line or an undeclared charge, and it is worth asking about.
        if quotation.total_amount is not None and quotation.lines:
            computed = sum(
                (line.line_total or Decimal(0) for line in quotation.lines), Decimal(0)
            ) + quotation.freight_amount + quotation.packing_amount + quotation.tooling_amount + quotation.other_charges - quotation.discount_amount
            if computed > 0:
                deviation = abs(computed - quotation.total_amount) / max(quotation.total_amount, Decimal(1))
                if deviation > Decimal("0.02"):
                    quotation.add_warning(
                        f"Stated total {quotation.total_amount} does not reconcile with the "
                        f"sum of the quoted lines and charges ({computed.quantize(Decimal('0.01'))}); "
                        f"request clarification"
                    )

        quotation.confidence -= Decimal("0.03") * min(len(quotation.warnings), 5)
        quotation.confidence = max(Decimal(0), min(quotation.confidence, Decimal(1)))


# ------------------------------------------------------------------ helpers

_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "line_number": ("line", "line_number", "line no", "item", "item no", "pos", "position", "sr", "s.no"),
    "material_code": ("material", "material code", "part", "part no", "part number", "article", "sku", "your ref"),
    "description": ("description", "desc", "item description", "product", "designation"),
    "part_number": ("our part", "supplier part", "mfr part", "manufacturer part", "offered part"),
    "quantity": ("qty", "quantity", "amount", "pcs"),
    "uom": ("uom", "unit", "units", "u/m", "unit of measure"),
    "unit_price": ("unit price", "price", "rate", "unit rate", "price/unit", "unit cost", "net price"),
    "price_per": ("per", "price per", "price basis", "per qty"),
    "currency": ("currency", "curr", "ccy"),
    "lead_time": ("lead time", "delivery", "leadtime", "lead time (days)", "delivery days"),
    "line_total": ("total", "line total", "amount", "extended", "value", "net value"),
    "notes": ("notes", "remark", "remarks", "comment", "comments"),
}


def _map_columns(header: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, cell in enumerate(header):
        normalized = re.sub(r"[^a-z0-9/. ]+", " ", cell.lower()).strip()
        normalized = re.sub(r"\s+", " ", normalized)
        for canonical, aliases in _COLUMN_ALIASES.items():
            if canonical in mapping:
                continue
            if normalized in aliases or any(normalized.startswith(a) for a in aliases):
                mapping[canonical] = index
                break
    return mapping


def _find_header_row(rows: list[list[str]]) -> int | None:
    for index, cells in enumerate(rows[:12]):
        lowered = [c.lower() for c in cells]
        if any("price" in c or "rate" in c for c in lowered) and any(
            ("qty" in c or "quantity" in c or "description" in c or "item" in c) for c in lowered
        ):
            return index
    return None


def _cell(cells: list[str], index: int | None) -> str:
    if index is None or index >= len(cells):
        return ""
    return cells[index].strip()


def _infer_line_number(
    material_code: str, description: str, rfq_lines: list[dict[str, Any]]
) -> int:
    """Match a supplier's row back to the RFQ line it answers."""
    if not rfq_lines:
        return 0
    code = (material_code or "").strip().upper()
    if code:
        for line in rfq_lines:
            if str(line.get("material_code", "")).strip().upper() == code:
                return int(line.get("line_number", 0))
    tokens = {t for t in re.split(r"\W+", (description or "").lower()) if len(t) >= 4}
    if not tokens:
        return 0
    best_line, best_overlap = 0, 0
    for line in rfq_lines:
        candidate = {
            t
            for t in re.split(r"\W+", str(line.get("description", "")).lower())
            if len(t) >= 4
        }
        overlap = len(tokens & candidate)
        if overlap > best_overlap:
            best_line, best_overlap = int(line.get("line_number", 0)), overlap
    return best_line if best_overlap >= 2 else 0


def _first(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _amount(text: str, label_pattern: str) -> Decimal | None:
    match = re.search(
        rf"(?:{label_pattern})[^\n\r:]{{0,20}}[:\-]?\s*(?:[A-Z]{{3}}\s*)?(?:[$€£¥₹]\s*)?({NUM})",
        text,
        re.IGNORECASE,
    )
    return _dec(match.group(1)) if match else None


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip().replace(" ", "")
    text = re.sub(r"[^\d.,\-]", "", text)
    if not text or text in ("-", ".", ","):
        return None
    if text.count(",") and text.count("."):
        text = (
            text.replace(",", "")
            if text.rfind(".") > text.rfind(",")
            else text.replace(".", "").replace(",", ".")
        )
    elif text.count(","):
        tail = text.rsplit(",", 1)[1]
        text = text.replace(",", "") if len(tail) == 3 else text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _parse_date(text: str) -> datetime | None:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _is_decline(text: str) -> bool:
    lowered = text.lower()
    return any(
        phrase in lowered
        for phrase in (
            "no bid", "not bidding", "decline to quote", "declining to quote",
            "unable to quote", "cannot quote", "we will not be quoting",
            "regret that we are unable", "not in a position to offer",
        )
    )
