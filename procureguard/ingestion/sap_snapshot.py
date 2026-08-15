"""SAP extract ingestion.

Reads a purchase-history export (an ME2N/EKKO×EKPO extract, or the equivalent
from any ERP) and normalises it into the mirror tables.

Import is idempotent by construction, because the same extract genuinely does
arrive twice:

* the **file** is content-hashed, so a re-upload is recognised and skipped;
* each **row** is hashed over its normalised business key, so overlapping
  exports deduplicate;
* superseded rows are closed with ``valid_to`` rather than deleted, so history
  is never destroyed by a correction.

Rejected rows are counted and reported rather than dropped silently: an importer
that quietly discards 3% of a spend file produces a benchmark nobody can trust.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from procureguard.domain.units import try_normalize_uom
from procureguard.observability import logger

log = logger(__name__)


@dataclass(frozen=True, slots=True)
class NormalizedPurchaseRow:
    row_hash: str
    material_code: str
    po_number: str
    po_line: str
    vendor_id: str
    vendor_name: str
    quantity: Decimal
    uom: str
    unit_price: Decimal
    currency: str
    order_date: datetime
    plant_code: str = ""
    material_group: str = ""
    material_description: str = ""
    price_unit: int = 1
    net_value: Decimal = Decimal(0)
    exchange_rate: Decimal = Decimal(1)
    delivery_date: datetime | None = None
    actual_delivery_date: datetime | None = None
    incoterm: str = ""
    payment_terms: str = ""
    deletion_indicator: bool = False
    contract_number: str = ""
    info_record_number: str = ""

    @property
    def unit_price_base(self) -> Decimal:
        return self.unit_price * self.exchange_rate / max(self.price_unit, 1)


@dataclass(slots=True)
class ImportReport:
    source_name: str
    content_hash: str
    rows_read: int = 0
    rows_accepted: int = 0
    rows_deduplicated: int = 0
    rows_rejected: int = 0
    rejections: list[dict[str, Any]] = field(default_factory=list)

    def reject(self, line_number: int, reason: str, raw: dict[str, Any]) -> None:
        self.rows_rejected += 1
        # Cap the detail; a badly-formed file should not produce a 200 MB report.
        if len(self.rejections) < 500:
            self.rejections.append(
                {"line": line_number, "reason": reason, "row": dict(list(raw.items())[:12])}
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "content_hash": self.content_hash,
            "rows_read": self.rows_read,
            "rows_accepted": self.rows_accepted,
            "rows_deduplicated": self.rows_deduplicated,
            "rows_rejected": self.rows_rejected,
            "rejection_rate_pct": (
                round(100.0 * self.rows_rejected / self.rows_read, 2) if self.rows_read else 0.0
            ),
            "rejections": self.rejections[:50],
        }


# SAP field names alongside the plain-English ones, because both turn up.
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "material_code": ("material_code", "matnr", "material", "material_number"),
    "po_number": ("po_number", "ebeln", "purchase_order", "document_number"),
    "po_line": ("po_line", "ebelp", "item", "line_item"),
    "vendor_id": ("vendor_id", "lifnr", "supplier", "vendor"),
    "vendor_name": ("vendor_name", "name1", "supplier_name"),
    "quantity": ("quantity", "menge", "order_quantity"),
    "uom": ("uom", "meins", "unit", "order_unit"),
    "unit_price": ("unit_price", "netpr", "net_price", "price"),
    "currency": ("currency", "waers", "document_currency"),
    "order_date": ("order_date", "aedat", "bedat", "document_date"),
    "plant_code": ("plant_code", "werks", "plant"),
    "material_group": ("material_group", "matkl"),
    "material_description": ("material_description", "txz01", "short_text", "description"),
    "price_unit": ("price_unit", "peinh"),
    "net_value": ("net_value", "netwr", "net_order_value"),
    "exchange_rate": ("exchange_rate", "wkurs", "fx_rate"),
    "delivery_date": ("delivery_date", "eindt", "statistical_delivery_date"),
    "actual_delivery_date": ("actual_delivery_date", "gr_date", "goods_receipt_date"),
    "incoterm": ("incoterm", "inco1", "incoterms"),
    "payment_terms": ("payment_terms", "zterm", "terms_of_payment"),
    "deletion_indicator": ("deletion_indicator", "loekz", "deleted"),
    "contract_number": ("contract_number", "konnr", "outline_agreement"),
    "info_record_number": ("info_record_number", "infnr"),
}

_LOOKUP = {alias: canonical for canonical, aliases in _COLUMN_ALIASES.items() for alias in aliases}

REQUIRED = (
    "material_code", "po_number", "po_line", "vendor_id",
    "quantity", "uom", "unit_price", "currency", "order_date",
)

_DATE_FORMATS = (
    "%Y-%m-%d", "%Y%m%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
)


class SapPurchaseSnapshotParser:
    """Parses and normalises a purchase-history extract."""

    # Retained for the original call sites.
    REQUIRED = set(REQUIRED)

    def content_hash(self, content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def parse_csv(
        self, content: bytes, *, source_name: str = "extract.csv", strict: bool = True
    ) -> tuple[NormalizedPurchaseRow, ...]:
        rows, report = self.parse_with_report(content, source_name=source_name)
        if strict and report.rows_read and not rows:
            raise ValueError(
                f"No usable rows in {source_name}: "
                + "; ".join(r["reason"] for r in report.rejections[:3])
            )
        return rows

    def parse_with_report(
        self, content: bytes, *, source_name: str = "extract.csv"
    ) -> tuple[tuple[NormalizedPurchaseRow, ...], ImportReport]:
        report = ImportReport(source_name=source_name, content_hash=self.content_hash(content))
        text = content.decode("utf-8-sig", errors="replace")

        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)

        mapping = self._map_columns(reader.fieldnames or [])
        missing = [name for name in REQUIRED if name not in mapping]
        if missing:
            raise ValueError(f"Missing SAP export columns: {sorted(missing)}")

        accepted: list[NormalizedPurchaseRow] = []
        seen: set[str] = set()

        for line_number, raw in enumerate(reader, start=2):
            report.rows_read += 1
            values = {
                canonical: (raw.get(source) or "").strip()
                for canonical, source in mapping.items()
            }
            try:
                row = self._normalize(values)
            except (ValueError, InvalidOperation) as exc:
                report.reject(line_number, str(exc)[:200], raw)
                continue

            if row.row_hash in seen:
                report.rows_deduplicated += 1
                continue
            seen.add(row.row_hash)
            accepted.append(row)
            report.rows_accepted += 1

        if report.rows_rejected:
            log.warning(
                "sap_import_rejected_rows",
                source_name=source_name,
                rejected=report.rows_rejected,
                read=report.rows_read,
                first_reason=report.rejections[0]["reason"] if report.rejections else "",
            )
        log.info("sap_import_parsed", **{k: v for k, v in report.to_dict().items() if k != "rejections"})
        return tuple(accepted), report

    # ---------------------------------------------------------------- internals
    @staticmethod
    def _map_columns(fieldnames: list[str]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for name in fieldnames:
            if not name:
                continue
            key = name.strip().lower().replace(" ", "_").replace("-", "_").lstrip("/")
            canonical = _LOOKUP.get(key)
            if canonical and canonical not in mapping:
                mapping[canonical] = name
        return mapping

    def _normalize(self, values: dict[str, str]) -> NormalizedPurchaseRow:
        material_code = values["material_code"].strip().upper()
        po_number = values["po_number"].strip()
        po_line = values["po_line"].strip().lstrip("0") or "0"
        vendor_id = values["vendor_id"].strip().upper()

        for name in ("material_code", "po_number", "vendor_id"):
            if not values[name].strip():
                raise ValueError(f"{name} is empty")

        quantity = _decimal(values["quantity"], "quantity")
        unit_price = _decimal(values["unit_price"], "unit_price")
        if quantity <= 0:
            raise ValueError(f"quantity must be positive, got {quantity}")
        if unit_price < 0:
            raise ValueError(f"unit_price cannot be negative, got {unit_price}")

        currency = values["currency"].strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError(f"currency {currency!r} is not an ISO 4217 code")

        order_date = _date(values["order_date"], "order_date")
        price_unit = max(int(_decimal(values.get("price_unit") or "1", "price_unit")), 1)
        exchange_rate = _decimal(values.get("exchange_rate") or "1", "exchange_rate") or Decimal(1)
        net_value = (
            _decimal(values["net_value"], "net_value")
            if values.get("net_value")
            else (unit_price / price_unit * quantity)
        )

        # Hash the business key, not the whole row: a corrected description must
        # not create a duplicate line.
        row_hash = hashlib.sha256(
            "|".join(
                [material_code, po_number, po_line, vendor_id, str(quantity), currency,
                 order_date.date().isoformat()]
            ).encode()
        ).hexdigest()

        return NormalizedPurchaseRow(
            row_hash=row_hash,
            material_code=material_code,
            po_number=po_number,
            po_line=po_line,
            vendor_id=vendor_id,
            vendor_name=values.get("vendor_name", "").strip(),
            quantity=quantity,
            uom=try_normalize_uom(values["uom"], values["uom"].strip().upper()),
            unit_price=unit_price,
            currency=currency,
            order_date=order_date,
            plant_code=values.get("plant_code", "").strip(),
            material_group=values.get("material_group", "").strip().upper(),
            material_description=values.get("material_description", "").strip(),
            price_unit=price_unit,
            net_value=net_value,
            exchange_rate=exchange_rate,
            delivery_date=_optional_date(values.get("delivery_date")),
            actual_delivery_date=_optional_date(values.get("actual_delivery_date")),
            incoterm=values.get("incoterm", "").strip().upper(),
            payment_terms=values.get("payment_terms", "").strip().upper(),
            deletion_indicator=values.get("deletion_indicator", "").strip().upper() in ("X", "TRUE", "1", "Y"),
            contract_number=values.get("contract_number", "").strip(),
            info_record_number=values.get("info_record_number", "").strip(),
        )


def _decimal(value: str, field_name: str) -> Decimal:
    text = (value or "").strip()
    if not text:
        return Decimal(0)
    # SAP exports use both 1,234.56 and 1.234,56, and trailing-minus for credits.
    text = text.replace(" ", "").replace(" ", "")
    if text.endswith("-"):
        text = "-" + text[:-1]
    if "," in text and "." in text:
        text = text.replace(",", "") if text.rfind(".") > text.rfind(",") else text.replace(".", "").replace(",", ".")
    elif "," in text:
        tail = text.rsplit(",", 1)[1]
        text = text.replace(",", "") if len(tail) == 3 else text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} {value!r} is not a number") from exc


def _date(value: str, field_name: str) -> datetime:
    parsed = _optional_date(value)
    if parsed is None:
        raise ValueError(f"{field_name} {value!r} is not a recognised date")
    return parsed


def _optional_date(value: str | None) -> datetime | None:
    text = (value or "").strip()
    if not text or text in ("00000000", "0000-00-00"):
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
