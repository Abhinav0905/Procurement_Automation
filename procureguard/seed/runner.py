"""Seed orchestration.

Loads the whole synthetic enterprise into CockroachDB. Purchase history and
goods receipts are processed in chunks rather than materialised in full, so
seeding a million PO lines has bounded memory cost.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from procureguard.config import get_settings
from procureguard.infrastructure.db.session import get_engine
from procureguard.infrastructure.db.vector import native_vector_enabled
from procureguard.observability import logger
from procureguard.seed.catalog import CAPABILITY_TAGS
from procureguard.seed.generator import GeneratedMaterial, GeneratedVendor, SeedGenerator
from procureguard.seed.loader import BulkLoader, new_id, utcnow, vector_literal

log = logger(__name__)

PO_CHUNK = 20_000

# Ordered so that deletes never strand a dependent row.
RESET_ORDER = [
    "decision_evidence", "decisions", "compliance_assessments", "normalized_offers",
    "bid_rankings", "negotiation_targets", "negotiation_rounds",
    "po_recommendation_lines", "po_recommendations", "info_record_proposals",
    "quotation_lines", "quotations", "rfq_invitations", "rfq_lines", "rfqs",
    "supplier_candidates", "requirements", "communications", "scheduled_reminders",
    "approvals", "audit_log", "idempotency_keys", "security_findings",
    "claim_conflicts", "claims", "document_chunks", "document_versions", "documents",
    "purchase_requisition_lines", "purchase_requisitions", "sourcing_cases",
    "goods_receipt_history", "purchase_history", "sap_snapshots",
    "info_records", "contracts", "source_list", "material_alternate_units",
    "material_plants", "materials", "vendor_contacts", "vendors",
    "freight_rates", "fx_rates", "plants", "users",
]

ANALYZE_TABLES = [
    "purchase_history", "goods_receipt_history", "materials", "material_plants",
    "vendors", "info_records", "source_list", "fx_rates",
]


@dataclass(slots=True)
class SeedReport:
    scale: str
    seed: int
    duration_seconds: float = 0.0
    counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scale": self.scale,
            "seed": self.seed,
            "duration_seconds": round(self.duration_seconds, 2),
            "row_counts": self.counts,
            "total_rows": sum(self.counts.values()),
        }


def seed_database(
    *,
    scale: str = "medium",
    seed: int | None = None,
    reset: bool = False,
    tenant_id: str = "",
    embed: bool = True,
) -> SeedReport:
    settings = get_settings()
    engine = get_engine()
    generator = SeedGenerator(
        scale=scale,
        seed=seed if seed is not None else settings.seed_random_seed,
        tenant_id=tenant_id or settings.default_tenant_id,
    )
    loader = BulkLoader(engine)
    report = SeedReport(scale=scale, seed=generator.rng.getstate()[1][0])
    started = time.perf_counter()

    if reset:
        log.info("seed_reset_starting", tables=len(RESET_ORDER))
        loader.truncate(RESET_ORDER)

    native = native_vector_enabled()
    embedder = _embedder() if embed else None

    # ── reference data ──────────────────────────────────────────────────────
    report.counts["plants"] = loader.copy_rows(
        "plants",
        ["id", "tenant_id", "plant_code", "name", "company_code", "country", "city",
         "currency", "timezone", "purchasing_org", "purchasing_group", "created_at", "updated_at"],
        (_stamp(row) for row in generator.generate_plants()),
    )
    report.counts["users"] = loader.copy_rows(
        "users",
        ["id", "tenant_id", "actor_id", "email", "display_name", "roles", "department",
         "plant_scope", "approval_limit_base", "active", "external_subject", "api_key_hash",
         "created_at", "updated_at"],
        (_stamp({**row, "external_subject": "", "api_key_hash": ""}) for row in generator.generate_users()),
    )
    report.counts["fx_rates"] = loader.copy_rows(
        "fx_rates",
        ["id", "tenant_id", "base_currency", "quote_currency", "rate", "as_of", "source", "created_at"],
        ({**row, "id": new_id(), "created_at": utcnow()} for row in generator.generate_fx_rates()),
    )
    report.counts["freight_rates"] = loader.copy_rows(
        "freight_rates",
        ["id", "tenant_id", "origin_country", "destination_plant", "mode", "cost_per_kg",
         "cost_per_shipment", "currency", "transit_days", "customs_clearance_cost",
         "insurance_pct", "duty_rate_pct", "created_at", "updated_at"],
        (_stamp(row) for row in generator.generate_freight_rates()),
    )

    # ── material master ─────────────────────────────────────────────────────
    materials = generator.generate_materials()
    log.info("seeding_materials", count=len(materials))
    report.counts["materials"] = loader.copy_rows(
        "materials",
        ["id", "tenant_id", "material_code", "description", "long_description", "material_group",
         "material_group_text", "material_type", "industry_sector", "base_uom", "order_uom",
         "status", "procurement_type", "successor_material_code", "manufacturer",
         "manufacturer_part_number", "unspsc_code", "hs_code", "net_weight_kg", "hazardous",
         "serial_controlled", "batch_controlled", "quality_inspection_required",
         "specification_reference", "drawing_number", "revision", "attributes", "search_text",
         "embedding", "created_by", "created_at", "updated_at"],
        _material_rows(generator, materials, embedder, native),
    )
    report.counts["material_plants"] = loader.copy_rows(
        "material_plants",
        ["id", "tenant_id", "material_code", "plant_code", "status", "mrp_controller",
         "purchasing_group", "planned_delivery_days", "goods_receipt_processing_days",
         "safety_stock", "reorder_point", "minimum_lot_size", "rounding_value",
         "standard_price", "moving_average_price", "price_unit", "currency",
         "valuation_class", "unrestricted_stock", "created_at", "updated_at"],
        _material_plant_rows(generator, materials),
    )
    report.counts["material_alternate_units"] = loader.copy_rows(
        "material_alternate_units",
        ["id", "tenant_id", "material_code", "alt_uom", "numerator", "denominator",
         "base_uom", "created_at", "updated_at"],
        _alternate_unit_rows(generator, materials),
    )

    # ── vendor master ───────────────────────────────────────────────────────
    vendors = generator.generate_vendors()
    log.info("seeding_vendors", count=len(vendors))
    report.counts["vendors"] = loader.copy_rows(
        "vendors",
        ["id", "tenant_id", "vendor_id", "name", "legal_name", "status", "country", "region",
         "city", "address_line1", "postal_code", "currency", "default_incoterm",
         "default_incoterm_location", "payment_terms", "tax_id", "duns_number", "email",
         "phone", "website", "qualified", "qualification_expires_on", "iso9001_certified",
         "iso14001_certified", "iatf16949_certified", "certifications", "capability_tags",
         "on_time_delivery_pct", "quality_ppm", "quality_rejection_pct", "responsiveness_score",
         "average_quote_turnaround_days", "quote_response_rate_pct", "financial_risk",
         "geopolitical_risk", "risk_notes", "blocked_reason", "spend_ytd_base",
         "search_text", "embedding", "created_at", "updated_at"],
        _vendor_rows(generator, vendors, embedder, native),
    )
    report.counts["vendor_contacts"] = loader.copy_rows(
        "vendor_contacts",
        ["id", "tenant_id", "vendor_id", "name", "email", "phone", "role",
         "is_primary_rfq_contact", "language", "active", "created_at", "updated_at"],
        _contact_rows(vendors),
    )

    # ── sourcing relationships ──────────────────────────────────────────────
    generator.assign_suppliers()
    report.counts["source_list"] = loader.copy_rows(
        "source_list",
        ["id", "tenant_id", "material_code", "plant_code", "vendor_id", "valid_from",
         "valid_to", "fixed_source", "blocked", "mrp_relevant", "approval_reference",
         "created_at", "updated_at"],
        (_stamp(row) for row in generator.generate_source_list()),
    )

    # ── purchase history and goods receipts, in chunks ──────────────────────
    snapshot_id = new_id()
    content_hash = hashlib.sha256(
        f"{generator.tenant_id}|{scale}|{generator.start_date}|{generator.end_date}".encode()
    ).hexdigest()
    loader.copy_rows(
        "sap_snapshots",
        ["id", "tenant_id", "source_name", "extract_type", "content_hash", "storage_uri",
         "recorded_at", "imported_at", "row_count", "rows_inserted", "rows_deduplicated",
         "rows_rejected", "status", "rejection_report", "created_at", "updated_at"],
        [
            {
                "id": snapshot_id,
                "tenant_id": generator.tenant_id,
                "source_name": f"ME2N_EKPO_EXTRACT_{generator.end_date:%Y%m%d}.csv",
                "extract_type": "PURCHASE_HISTORY",
                "content_hash": content_hash,
                "storage_uri": "",
                "recorded_at": datetime(
                    generator.end_date.year, generator.end_date.month, generator.end_date.day, tzinfo=UTC
                ),
                "imported_at": utcnow(),
                "row_count": generator.po_line_target,
                "rows_inserted": generator.po_line_target,
                "rows_deduplicated": 0,
                "rows_rejected": 0,
                "status": "COMPLETED",
                "rejection_report": {},
                "created_at": utcnow(),
                "updated_at": utcnow(),
            }
        ],
    )
    report.counts["sap_snapshots"] = 1

    log.info("seeding_purchase_history", target=generator.po_line_target)
    po_written = 0
    gr_written = 0
    po_columns = [
        "id", "tenant_id", "snapshot_id", "row_hash", "po_number", "po_line", "po_type",
        "material_code", "material_description", "material_group", "plant_code",
        "purchasing_org", "purchasing_group", "vendor_id", "vendor_name", "quantity", "uom",
        "unit_price", "price_unit", "net_value", "currency", "exchange_rate", "net_value_base",
        "base_currency", "order_date", "delivery_date", "actual_delivery_date", "incoterm",
        "incoterm_location", "payment_terms", "delivered_quantity", "invoiced_quantity",
        "rejected_quantity", "on_time", "days_late", "deletion_indicator",
        "requisition_number", "contract_number", "info_record_number", "created_by",
        "valid_from", "valid_to", "learned_at",
    ]
    gr_columns = [
        "id", "tenant_id", "snapshot_id", "row_hash", "material_document", "document_line",
        "po_number", "po_line", "material_code", "vendor_id", "plant_code", "movement_type",
        "quantity", "uom", "posting_date", "scheduled_date", "days_late", "inspection_result",
        "rejected_quantity", "rejection_reason", "batch", "learned_at",
    ]

    for chunk in _chunk(generator.generate_purchase_history(snapshot_id), PO_CHUNK):
        po_written += loader.copy_rows(
            "purchase_history", po_columns, ({**row, "id": new_id()} for row in chunk)
        )
        gr_written += loader.copy_rows(
            "goods_receipt_history",
            gr_columns,
            (
                {**row, "id": new_id()}
                for row in generator.generate_goods_receipts(chunk, snapshot_id)
            ),
        )
        log.info("purchase_history_progress", po_lines=po_written, goods_receipts=gr_written)

    report.counts["purchase_history"] = po_written
    report.counts["goods_receipt_history"] = gr_written

    # ── derived purchasing master data ──────────────────────────────────────
    report.counts["info_records"] = loader.copy_rows(
        "info_records",
        ["id", "tenant_id", "info_record_number", "material_code", "vendor_id", "plant_code",
         "purchasing_org", "net_price", "currency", "price_unit", "order_uom",
         "minimum_order_quantity", "planned_delivery_days", "incoterm", "incoterm_location",
         "payment_terms", "tax_code", "price_scales", "valid_from", "valid_to",
         "source_case_id", "source_quotation_id", "is_active", "superseded_by_id",
         "created_at", "updated_at"],
        (
            _stamp({**row, "tax_code": "", "source_case_id": "", "source_quotation_id": "",
                    "superseded_by_id": ""})
            for row in generator.generate_info_records()
        ),
    )
    report.counts["contracts"] = loader.copy_rows(
        "contracts",
        ["id", "tenant_id", "contract_number", "vendor_id", "contract_type", "description",
         "target_value", "released_value", "currency", "valid_from", "valid_to",
         "payment_terms", "incoterm", "price_protection_clause", "materials", "is_active",
         "created_at", "updated_at"],
        (_stamp(row) for row in generator.generate_contracts()),
    )

    loader.analyze(ANALYZE_TABLES)
    report.duration_seconds = time.perf_counter() - started
    log.info("seed_complete", **report.to_dict())
    return report


# ────────────────────────────────────────────────────────────────── row builders

def _material_rows(
    generator: SeedGenerator,
    materials: list[GeneratedMaterial],
    embedder: Any,
    native: bool,
) -> Iterator[dict[str, Any]]:
    for material in materials:
        search_text = " ".join(
            filter(
                None,
                [
                    material.material_code,
                    material.description,
                    material.group.text,
                    material.manufacturer,
                    material.manufacturer_part_number,
                    " ".join(str(v) for v in material.attributes.values()),
                ],
            )
        )
        yield _stamp(
            {
                "tenant_id": generator.tenant_id,
                "material_code": material.material_code,
                "description": material.description,
                "long_description": material.long_description,
                "material_group": material.group.code,
                "material_group_text": material.group.text,
                "material_type": material.group.material_type,
                "industry_sector": "M",
                "base_uom": material.base_uom,
                "order_uom": material.base_uom,
                "status": material.status,
                "procurement_type": (
                    "INTERNAL" if generator.rng.random() < 0.03 else "EXTERNAL"
                ),
                "successor_material_code": material.successor,
                "manufacturer": material.manufacturer,
                "manufacturer_part_number": material.manufacturer_part_number,
                "unspsc_code": f"{generator.rng.randint(20000000, 49999999)}",
                "hs_code": f"{generator.rng.randint(7300, 8548)}.{generator.rng.randint(10, 99)}",
                "net_weight_kg": material.weight_kg,
                "hazardous": material.hazardous,
                "serial_controlled": material.group.code in ("MG-HYD", "MG-ELECTRONIC")
                and generator.rng.random() < 0.2,
                "batch_controlled": material.batch_controlled,
                "quality_inspection_required": material.quality_inspection,
                "specification_reference": (
                    f"SPEC-{material.material_code}" if material.drawing_number else ""
                ),
                "drawing_number": material.drawing_number,
                "revision": f"R{generator.rng.randint(0, 6)}",
                "attributes": material.attributes,
                "search_text": search_text.lower(),
                "embedding": vector_literal(
                    embedder.embed(search_text) if embedder else None, native
                ),
                "created_by": "SAP_IMPORT",
            }
        )


def _material_plant_rows(
    generator: SeedGenerator, materials: list[GeneratedMaterial]
) -> Iterator[dict[str, Any]]:
    for material in materials:
        for plant_code in material.plants:
            standard = (material.base_price * Decimal("1.04")).quantize(Decimal("0.0001"))
            yield _stamp(
                {
                    "tenant_id": generator.tenant_id,
                    "material_code": material.material_code,
                    "plant_code": plant_code,
                    "status": (
                        material.status
                        if material.status != "OBSOLETE"
                        else "BLOCKED_FOR_PROCUREMENT"
                    ),
                    "mrp_controller": f"{generator.rng.randint(1, 40):03d}",
                    "purchasing_group": f"{generator.rng.randint(1, 12):03d}",
                    "planned_delivery_days": material.lead_time_days,
                    "goods_receipt_processing_days": generator.rng.choice([1, 1, 2, 3]),
                    "safety_stock": Decimal(generator.rng.randrange(0, 500, 10)),
                    "reorder_point": Decimal(generator.rng.randrange(0, 1000, 25)),
                    "minimum_lot_size": Decimal(
                        generator.rng.choice([1, 1, 1, 5, 10, 25, 50, 100])
                    ),
                    "rounding_value": Decimal(generator.rng.choice([1, 1, 1, 5, 10, 25])),
                    "standard_price": standard,
                    "moving_average_price": (
                        standard * Decimal(str(round(generator.rng.uniform(0.94, 1.08), 4)))
                    ).quantize(Decimal("0.0001")),
                    "price_unit": 1,
                    "currency": "USD",
                    "valuation_class": generator.rng.choice(["3000", "3040", "7900"]),
                    "unrestricted_stock": Decimal(generator.rng.randrange(0, 4000, 5)),
                }
            )


def _alternate_unit_rows(
    generator: SeedGenerator, materials: list[GeneratedMaterial]
) -> Iterator[dict[str, Any]]:
    for material in materials:
        if material.alternate_unit is None:
            continue
        alt_uom, factor, base_uom = material.alternate_unit
        yield _stamp(
            {
                "tenant_id": generator.tenant_id,
                "material_code": material.material_code,
                "alt_uom": alt_uom,
                "numerator": factor,
                "denominator": Decimal(1),
                "base_uom": base_uom,
            }
        )


def _vendor_rows(
    generator: SeedGenerator,
    vendors: list[GeneratedVendor],
    embedder: Any,
    native: bool,
) -> Iterator[dict[str, Any]]:
    for vendor in vendors:
        tags = sorted(
            {tag for group in vendor.groups for tag in CAPABILITY_TAGS.get(group, ())}
        )
        search_text = " ".join(
            [vendor.name, vendor.country, vendor.city, *tags, *vendor.certifications]
        )
        rejection = Decimal(str(round(vendor.quality_ppm / 10_000, 4)))
        yield _stamp(
            {
                "tenant_id": generator.tenant_id,
                "vendor_id": vendor.vendor_id,
                "name": vendor.name,
                "legal_name": vendor.name,
                "status": vendor.status,
                "country": vendor.country,
                "region": _region_of(vendor.country),
                "city": vendor.city,
                "address_line1": f"{generator.rng.randint(1, 400)} Industrial Way",
                "postal_code": f"{generator.rng.randint(10000, 99999)}",
                "currency": vendor.currency,
                "default_incoterm": vendor.incoterm,
                "default_incoterm_location": vendor.city,
                "payment_terms": vendor.payment_terms,
                "tax_id": f"{vendor.country}{generator.rng.randint(100000000, 999999999)}",
                "duns_number": f"{generator.rng.randint(100000000, 999999999)}",
                "email": f"sales@{vendor.email_domain}",
                "phone": f"+{generator.rng.randint(1, 99)} {generator.rng.randint(1000000, 9999999)}",
                "website": f"https://www.{vendor.email_domain}",
                "qualified": vendor.status == "ACTIVE",
                "qualification_expires_on": datetime(
                    generator.end_date.year + generator.rng.choice([-1, 1, 1, 2]),
                    generator.rng.randint(1, 12),
                    generator.rng.randint(1, 28),
                    tzinfo=UTC,
                ),
                "iso9001_certified": any("9001" in c for c in vendor.certifications),
                "iso14001_certified": any("14001" in c for c in vendor.certifications),
                "iatf16949_certified": any("16949" in c for c in vendor.certifications),
                "certifications": vendor.certifications,
                "capability_tags": tags,
                "on_time_delivery_pct": Decimal(str(round(vendor.on_time_pct, 2))),
                "quality_ppm": vendor.quality_ppm,
                "quality_rejection_pct": rejection,
                "responsiveness_score": Decimal(str(round(vendor.response_rate, 2))),
                "average_quote_turnaround_days": Decimal(str(round(vendor.turnaround_days, 2))),
                "quote_response_rate_pct": Decimal(str(round(vendor.response_rate, 2))),
                "financial_risk": vendor.financial_risk,
                "geopolitical_risk": vendor.geopolitical_risk,
                "risk_notes": "",
                "blocked_reason": (
                    "Quality escalation open; supply blocked pending corrective action"
                    if vendor.status == "BLOCKED"
                    else ""
                ),
                "spend_ytd_base": Decimal(generator.rng.randrange(0, 2_000_000, 1_000)),
                "search_text": search_text.lower(),
                "embedding": vector_literal(
                    embedder.embed(search_text) if embedder else None, native
                ),
            }
        )


def _contact_rows(vendors: list[GeneratedVendor]) -> Iterator[dict[str, Any]]:
    settings = get_settings()
    for vendor in vendors:
        for contact in vendor.contacts:
            yield _stamp(
                {
                    "tenant_id": settings.default_tenant_id,
                    "vendor_id": vendor.vendor_id,
                    "name": contact["name"],
                    "email": contact["email"],
                    "phone": contact["phone"],
                    "role": contact["role"],
                    "is_primary_rfq_contact": contact["primary"] == True,  # noqa: E712
                    "language": "en",
                    "active": True,
                }
            )


def _stamp(row: dict[str, Any]) -> dict[str, Any]:
    now = utcnow()
    return {**row, "id": row.get("id") or new_id(), "created_at": now, "updated_at": now}


def _region_of(country: str) -> str:
    regions = {
        "EMEA": {"DE", "IT", "PL", "CZ", "ES", "SE", "GB", "TR"},
        "AMERICAS": {"US", "MX", "CA", "BR"},
        "APAC": {"CN", "IN", "JP", "KR", "VN"},
    }
    for name, members in regions.items():
        if country in members:
            return name
    return "OTHER"


def _chunk(iterator: Iterator[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for row in iterator:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _embedder() -> Any:
    from procureguard.infrastructure.factory import get_embedding_model

    return get_embedding_model()
