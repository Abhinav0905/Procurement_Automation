"""Parser tests: requisitions, specifications, quotations, SAP extracts."""

from __future__ import annotations

from decimal import Decimal

from procureguard.ingestion.parsers.pr_parser import PurchaseRequisitionParser
from procureguard.ingestion.parsers.quotation_parser import QuotationParser
from procureguard.ingestion.parsers.spec_parser import SpecificationParser
from procureguard.ingestion.parsers.text_extract import TextExtractor, chunk_text
from procureguard.ingestion.sap_snapshot import SapPurchaseSnapshotParser

# ── requisition parsing ──────────────────────────────────────────────────────

EMAIL_PR = b"""Hi team,

PR-778901 - urgent, line down.
Plant: 1000
Requested by: Dana Whitfield
Email: dana.whitfield@acme.example.com
Required by: 2026-10-01

We need:
- 250 pcs of VAL-1023 gate valve DN50 PN16
- 40 kg PTFE sealing tape
- 12 EA hydraulic pump HYD-2044, by 15/09/2026

Cost center: 4711
Thanks
"""


def test_email_requisition_is_parsed():
    result = PurchaseRequisitionParser().parse(EMAIL_PR, source_channel="EMAIL")
    pr = result.requisition
    assert result.source_format == "freetext"
    assert pr.pr_number == "PR-778901"
    assert pr.plant_code == "1000"
    assert pr.priority == "URGENT"
    assert len(pr.lines) == 3
    assert pr.lines[0].material_code == "VAL-1023"
    assert pr.lines[0].quantity == Decimal(250)


def test_per_line_date_beats_header_default():
    result = PurchaseRequisitionParser().parse(EMAIL_PR)
    lines = {line.line_number: line for line in result.requisition.lines}
    assert lines[3].required_date.date().isoformat() == "2026-09-15"
    assert lines[1].required_date.date().isoformat() == "2026-10-01"


def test_matched_date_is_stripped_from_description():
    result = PurchaseRequisitionParser().parse(EMAIL_PR)
    third = result.requisition.lines[2]
    assert "2026" not in third.description


def test_freetext_confidence_is_below_structured():
    freetext = PurchaseRequisitionParser().parse(EMAIL_PR)
    csv_payload = (
        b"PR Number: PR-9001\nPlant: 1000\nRequester: Sam\n"
        b"line,material,description,qty,uom,need_by,price,currency\n"
        b"10,VAL-1023,Gate valve DN50,250,PC,2026-09-30,142.50,USD\n"
    )
    structured = PurchaseRequisitionParser().parse(csv_payload, filename="pr.csv")
    assert structured.source_format == "csv"
    assert structured.confidence > freetext.confidence
    assert structured.requisition.lines[0].estimated_unit_price == Decimal("142.50")


def test_json_requisition_round_trips():
    payload = (
        b'{"pr_number":"PR-1","plant_code":"2000","requester":"Ana",'
        b'"lines":[{"line_number":10,"material_code":"BRG-1","quantity":100,"uom":"EA"}]}'
    )
    result = PurchaseRequisitionParser().parse(payload, media_type="application/json")
    assert result.source_format == "json"
    assert result.requisition.lines[0].material_code == "BRG-1"


def test_missing_unit_is_reported_not_assumed():
    payload = b'{"pr_number":"PR-2","plant_code":"1000","requester":"A","lines":[{"line_number":1,"material_code":"X-1","quantity":5}]}'
    result = PurchaseRequisitionParser().parse(payload, media_type="application/json")
    assert any("unit of measure" in w.lower() for w in result.warnings)


# ── specification parsing ────────────────────────────────────────────────────

SPEC = """TECHNICAL SPECIFICATION - GATE VALVE DN50

3.1 Design
Nominal diameter: DN50
Body material: ASTM A216 WCB
Design pressure shall be minimum 16 bar
Operating temperature range: -20 to 200 °C
Wall thickness: 3.2 mm ± 0.2 mm
Maximum weight 45 kg

3.2 Compliance
The valve shall comply with ASME B16.34
Supplier must hold ISO 9001 certification
Material test certificate EN 10204 3.1 required

3.3 Finish
Surface finish Ra shall be maximum 3.2 µm
Body coating: epoxy / polyurethane / zinc primer
Stem seal should be PTFE

3.4 Delivery
Delivery within 8 weeks
Warranty minimum 24 months
"""


# A requisition typed by a plant engineer, using the column names an SAP export
# and a hand-built sheet actually carry: a serial column rather than "Item", "EOM"
# for the unit, a material group, and both a 40-character short text and a
# 200-character long text.
PLANT_SHEET_PR = b"""PR Number: PR-2026-0900
Plant: 1000
Requester: R. Menon

Sl.No.,Material Code,Item Description,Plant Code,Material Group,Storage Location,Quantity,EOM,Delivery Date,Long Text
10,SEL-00066,Rotary shaft seal 367mm EPDM,1000,MG-SEAL,0001,10,EA,2027-01-15,Rotary shaft seal for the cooling water pump shaft. Continuous duty at 80 deg C.
20,,SS 304 seamless pipe 50NB Sch 40,1000,MG-RAW,0002,60,M,2027-01-15,Seamless austenitic stainless pipe ASTM A312 TP304 Schedule 40. Mill certificate 3.1 required.
"""


def test_plant_sheet_columns_are_recognised():
    """Sl.No., EOM, Material Group and Long Text must all map.

    Before these aliases existed the serial column was ignored, so every line
    silently took its file position as its line number - which renumbers the
    references a buyer quotes back to the requester.
    """
    result = PurchaseRequisitionParser().parse(
        content=PLANT_SHEET_PR, filename="pr.csv", media_type="text/csv"
    )
    assert not result.warnings, result.warnings
    pr = result.requisition
    assert pr.pr_number == "PR-2026-0900"
    assert [line.line_number for line in pr.lines] == [10, 20]

    first = pr.lines[0]
    assert first.material_code == "SEL-00066"
    assert first.uom == "EA"
    assert first.quantity == Decimal(10)
    assert first.requested_material_group == "MG-SEAL"
    # The short text stays the description; the long text is kept separately
    # rather than overwriting it.
    assert first.description == "Rotary shaft seal 367mm EPDM"
    assert "Continuous duty" in first.notes

    second = pr.lines[1]
    assert second.free_text_only is True
    assert second.uom == "M"


def test_long_text_is_used_when_no_short_text_is_given():
    """A sheet carrying only a long text still has to yield something to match on."""
    content = (
        b"Sl.No.,Material Code,Long Text,Plant Code,Quantity,EOM\n"
        b"1,,Butt weld long radius elbow 90 degree 50NB SS 304 to ASTM A403,1000,24,EA\n"
    )
    result = PurchaseRequisitionParser().parse(
        content=content, filename="pr.csv", media_type="text/csv"
    )
    line = result.requisition.lines[0]
    assert line.description.startswith("Butt weld long radius elbow")
    assert line.free_text_only is True


def test_specification_yields_typed_requirements():
    requirements = SpecificationParser().extract(SPEC, source_location="SPEC-1")
    by_attribute = {r.attribute.lower(): r for r in requirements}
    assert len(requirements) >= 12

    pressure = by_attribute["design pressure"]
    assert pressure.operator == "GTE"
    assert pressure.target_numeric == Decimal(16)
    assert pressure.uom == "BAR"

    thickness = by_attribute["wall thickness"]
    assert thickness.operator == "TOLERANCE"
    assert thickness.tolerance_plus == Decimal("0.2")
    assert thickness.uom == "MM"


def test_section_tracking_survives_unit_lookalike_headings():
    """'3.2 Compliance' is a heading, not '3.2 C' as a temperature."""
    requirements = SpecificationParser().extract(SPEC, source_location="S")
    sections = {r.source_location for r in requirements}
    assert "S §3.2" in sections
    assert "S §3.4" in sections


def test_letter_prefixed_standards_are_captured():
    requirements = SpecificationParser().extract(SPEC)
    assert any("ASME B16.34" in r.attribute for r in requirements)


def test_binding_language_sets_obligation():
    requirements = {r.attribute.lower(): r for r in SpecificationParser().extract(SPEC)}
    assert requirements["design pressure"].obligation == "MANDATORY"
    assert requirements["stem seal"].obligation == "DESIRABLE"


def test_lead_time_and_warranty_directions_differ():
    requirements = {r.attribute.lower(): r for r in SpecificationParser().extract(SPEC)}
    assert requirements["delivery"].operator == "LTE"
    assert requirements["warranty"].operator == "GTE"


# ── quotation parsing ────────────────────────────────────────────────────────

RFQ_LINES = [
    {"line_number": 10, "material_code": "VAL-1023", "description": "Gate valve DN50", "quantity": 250, "uom": "EA"},
    {"line_number": 20, "material_code": "FST-3310", "description": "Hex bolt M8", "quantity": 5000, "uom": "EA"},
]

QUOTE = """Dear Procurement,

Quotation No: QT-88213
Currency: EUR
Incoterms: FCA Rotterdam
Payment terms: 2/10 net 45
Validity: 60 days
Lead time: 6 weeks from order confirmation
Warranty: 24 months

Item 10: 250 EA at EUR 131.40 each
Item 20: 5000 EA at EUR 88.00 per 100 EA

Freight: EUR 850
Packing: EUR 120

REQ-003: Compliant, 20 bar
REQ-009: Yes - ISO 9001:2015 certified

Best regards
"""


def test_quotation_header_is_extracted():
    quote = QuotationParser().parse(QUOTE, rfq_lines=RFQ_LINES)
    assert quote.currency == "EUR"
    assert quote.incoterm == "FCA"
    assert quote.incoterm_location == "Rotterdam"
    assert quote.payment_terms == "2/10 net 45"
    assert quote.validity_days == 60
    assert quote.lead_time_days == 42
    assert quote.warranty_months == 24
    assert quote.freight_amount == Decimal(850)


def test_price_basis_per_hundred_is_preserved():
    """Losing 'per 100 EA' would overstate the bid by a factor of a hundred."""
    quote = QuotationParser().parse(QUOTE, rfq_lines=RFQ_LINES)
    lines = {line.rfq_line_number: line for line in quote.lines}
    assert lines[10].price_per_quantity == Decimal(1)
    assert lines[20].price_per_quantity == Decimal(100)
    assert lines[20].unit_price / lines[20].price_per_quantity == Decimal("0.88")


def test_requirement_answers_do_not_bleed_across_lines():
    """An unanswered 'REQ-x:' must not absorb the next line's answer."""
    text = "REQ-001:\nREQ-002: 20 bar\n"
    quote = QuotationParser().parse(text, default_currency="USD")
    assert quote.technical_answers.get("REQ-002") == "20 bar"
    assert "REQ-001" not in quote.technical_answers


def test_declines_are_detected():
    quote = QuotationParser().parse("Thank you, but we are unable to quote on this occasion.")
    assert quote.declined


def test_missing_commercial_terms_are_warned_not_guessed():
    quote = QuotationParser().parse("Item 10: 250 EA at USD 5.00 each", rfq_lines=RFQ_LINES)
    joined = " ".join(quote.warnings).lower()
    assert "incoterm" in joined
    assert "payment terms" in joined


def test_csv_template_response_is_parsed_with_high_confidence():
    csv_text = (
        "line_number,material_code,description,quantity,uom,unit_price,price_per_quantity,"
        "currency,lead_time_days,minimum_order_quantity,notes\n"
        "10,VAL-1023,Gate valve,250,EA,131.40,1,EUR,42,,\n"
    )
    quote = QuotationParser().parse(csv_text, rfq_lines=RFQ_LINES)
    assert quote.source_format == "csv_template"
    line = quote.lines[0]
    assert (line.rfq_line_number, line.unit_price, line.currency) == (10, Decimal("131.40"), "EUR")
    # A bare template carries no Incoterm, payment terms, validity or lead time,
    # and each omission legitimately costs confidence - so the structured parse
    # is compared against the free-text parse of the same content, not to 1.0.
    freetext = QuotationParser().parse(
        "Item 10: 250 EA at EUR 131.40 each", rfq_lines=RFQ_LINES
    )
    assert quote.confidence > freetext.confidence


# ── text extraction and chunking ─────────────────────────────────────────────

def test_csv_extraction_and_structure_aware_chunking():
    extracted = TextExtractor().extract(
        b"Attribute,Value\nPressure,16 bar\nMaterial,SS316", filename="x.csv"
    )
    assert "Pressure" in extracted.text

    chunks = chunk_text(
        "1. SCOPE\nCovers the valve.\n\n2. DESIGN\nPressure 16 bar. " + ("Filler text. " * 200)
    )
    assert len(chunks) > 1
    assert chunks[0].section_path.startswith("1")


def test_unsupported_pdf_reports_rather_than_returning_empty():
    result = TextExtractor().extract(b"%PDF-1.4 not really a pdf", filename="d.pdf")
    assert result.is_empty
    assert result.warnings, "a failed extraction must say so"


# ── SAP extract ──────────────────────────────────────────────────────────────

def test_sap_extract_deduplicates_and_normalises():
    raw = (
        b"material_code,po_number,po_line,vendor_id,vendor_name,quantity,uom,unit_price,currency,order_date\n"
        b"M1,P1,10,V1,Vendor,2,EA,10.5,usd,2026-01-01\n"
        b"M1,P1,10,V1,Vendor,2,EA,10.5,usd,2026-01-01\n"
    )
    rows = SapPurchaseSnapshotParser().parse_csv(raw)
    assert len(rows) == 1
    assert rows[0].currency == "USD"
