"""CockroachDB schema.

Organised into the three data classes the architecture doc calls out:

1. **Enterprise relational** - the SAP export mirror. Material master, vendor
   master, purchasing info records, PO/GR history. Tens of millions of rows are
   expected; access is always indexed and bounded.
2. **Evidence knowledge** - immutable documents, versions, chunks, embeddings
   and atomic claims with provenance. Nothing here is ever updated in place.
3. **Agent state** - the durable case file: requisitions, requirements,
   shortlists, RFQs, quotations, evaluations, negotiations, approvals, audit.

Conventions
-----------
* UUID string PKs generated in the application. CockroachDB spreads these across
  ranges; monotonic sequences would hot-spot a single leaseholder.
* ``tenant_id`` on every business table, and it is the leading column of the
  hot indexes so multi-tenant scans stay in one key span.
* ``Numeric(18, 6)`` for money and quantity. Float money is a defect.
* ``TIMESTAMPTZ`` everywhere; the application only ever handles aware datetimes.
* The two highest-volume history tables carry no foreign keys by design - bulk
  import throughput matters more than referential enforcement on an append-only
  mirror whose integrity is guaranteed by the importer.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from procureguard.config import get_settings

from .vector import EmbeddingVector

MONEY = Numeric(18, 6)
QTY = Numeric(18, 6)
PCT = Numeric(9, 4)
SCORE = Numeric(9, 4)


def _json_safe(value: Any) -> Any:
    """Coerce Python values that JSON cannot represent.

    Decimals become strings rather than floats: these payloads carry prices, and
    round-tripping money through a float is exactly the defect the domain layer
    goes to some trouble to avoid.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in value]
    return value


class SafeJsonb(TypeDecorator):
    """JSONB that accepts Decimal and datetime without the caller sanitising.

    Every JSONB column in this schema stores analysis output, and analysis output
    is full of Decimals. Coercing at the column boundary means a new decision
    payload cannot fail to serialise at flush time - which was otherwise a
    runtime error a long way from its cause.
    """

    impl = JSONB
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        return _json_safe(value) if value is not None else None


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: SafeJsonb, list[Any]: SafeJsonb, Decimal: MONEY}


def uid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class TenantMixin:
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


# ═══════════════════════════════════════════════════════════════════════════
# 1. ENTERPRISE RELATIONAL - the SAP export mirror
# ═══════════════════════════════════════════════════════════════════════════


class SapSnapshotModel(Base, TimestampMixin, TenantMixin):
    """One ingested SAP extract. Content-hashed so re-imports are idempotent."""

    __tablename__ = "sap_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    extract_type: Mapped[str] = mapped_column(String(64), nullable=False, default="PURCHASE_HISTORY")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_uri: Mapped[str] = mapped_column(Text, default="")
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    rows_inserted: Mapped[int] = mapped_column(Integer, default=0)
    rows_deduplicated: Mapped[int] = mapped_column(Integer, default=0)
    rows_rejected: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="COMPLETED")
    rejection_report: Mapped[dict[str, Any]] = mapped_column(SafeJsonb, default=dict)

    __table_args__ = (
        UniqueConstraint("tenant_id", "content_hash", name="uq_snapshot_content"),
        Index("idx_snapshot_tenant_type_time", "tenant_id", "extract_type", "recorded_at"),
    )


class PlantModel(Base, TimestampMixin, TenantMixin):
    __tablename__ = "plants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    plant_code: Mapped[str] = mapped_column(String(8), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    company_code: Mapped[str] = mapped_column(String(8), default="1000")
    country: Mapped[str] = mapped_column(String(2), default="US")
    city: Mapped[str] = mapped_column(String(120), default="")
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    purchasing_org: Mapped[str] = mapped_column(String(8), default="1000")
    purchasing_group: Mapped[str] = mapped_column(String(8), default="001")

    __table_args__ = (UniqueConstraint("tenant_id", "plant_code", name="uq_plant_code"),)


class MaterialModel(Base, TimestampMixin, TenantMixin):
    """Material master (SAP MARA-equivalent), client level."""

    __tablename__ = "materials"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    material_code: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    long_description: Mapped[str] = mapped_column(Text, default="")
    material_group: Mapped[str] = mapped_column(String(20), index=True, default="")
    material_group_text: Mapped[str] = mapped_column(String(120), default="")
    material_type: Mapped[str] = mapped_column(String(8), default="ROH")
    industry_sector: Mapped[str] = mapped_column(String(4), default="M")
    base_uom: Mapped[str] = mapped_column(String(8), nullable=False, default="EA")
    order_uom: Mapped[str] = mapped_column(String(8), default="")
    status: Mapped[str] = mapped_column(String(40), default="ACTIVE", index=True)
    procurement_type: Mapped[str] = mapped_column(String(16), default="EXTERNAL")
    successor_material_code: Mapped[str] = mapped_column(String(40), default="")
    manufacturer: Mapped[str] = mapped_column(String(200), default="")
    manufacturer_part_number: Mapped[str] = mapped_column(String(120), default="", index=True)
    unspsc_code: Mapped[str] = mapped_column(String(20), default="")
    hs_code: Mapped[str] = mapped_column(String(20), default="")
    net_weight_kg: Mapped[Decimal | None] = mapped_column(QTY, nullable=True)
    hazardous: Mapped[bool] = mapped_column(Boolean, default=False)
    serial_controlled: Mapped[bool] = mapped_column(Boolean, default=False)
    batch_controlled: Mapped[bool] = mapped_column(Boolean, default=False)
    quality_inspection_required: Mapped[bool] = mapped_column(Boolean, default=False)
    specification_reference: Mapped[str] = mapped_column(String(200), default="")
    drawing_number: Mapped[str] = mapped_column(String(120), default="")
    revision: Mapped[str] = mapped_column(String(20), default="")
    attributes: Mapped[dict[str, Any]] = mapped_column(SafeJsonb, default=dict)
    search_text: Mapped[str] = mapped_column(Text, default="")
    embedding: Mapped[list[float] | None] = mapped_column(
        EmbeddingVector(get_settings().embedding_dimensions), nullable=True
    )
    created_by: Mapped[str] = mapped_column(String(120), default="SAP_IMPORT")

    __table_args__ = (
        UniqueConstraint("tenant_id", "material_code", name="uq_material_code"),
        Index("idx_material_group", "tenant_id", "material_group"),
        Index("idx_material_mpn", "tenant_id", "manufacturer_part_number"),
        Index("idx_material_status", "tenant_id", "status"),
    )


class MaterialPlantModel(Base, TimestampMixin, TenantMixin):
    """Plant-level extension (SAP MARC). A material not extended to the plant
    cannot be procured for that plant - a real and frequent PR rejection."""

    __tablename__ = "material_plants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    material_code: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    plant_code: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="ACTIVE")
    mrp_controller: Mapped[str] = mapped_column(String(8), default="")
    purchasing_group: Mapped[str] = mapped_column(String(8), default="")
    planned_delivery_days: Mapped[int] = mapped_column(Integer, default=14)
    goods_receipt_processing_days: Mapped[int] = mapped_column(Integer, default=2)
    safety_stock: Mapped[Decimal] = mapped_column(QTY, default=Decimal(0))
    reorder_point: Mapped[Decimal] = mapped_column(QTY, default=Decimal(0))
    minimum_lot_size: Mapped[Decimal] = mapped_column(QTY, default=Decimal(1))
    rounding_value: Mapped[Decimal] = mapped_column(QTY, default=Decimal(1))
    standard_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    moving_average_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    price_unit: Mapped[int] = mapped_column(Integer, default=1)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    valuation_class: Mapped[str] = mapped_column(String(8), default="3000")
    unrestricted_stock: Mapped[Decimal] = mapped_column(QTY, default=Decimal(0))

    __table_args__ = (
        UniqueConstraint("tenant_id", "material_code", "plant_code", name="uq_material_plant"),
        Index("idx_material_plant_lookup", "tenant_id", "plant_code", "material_code"),
    )


class MaterialAlternateUnitModel(Base, TimestampMixin, TenantMixin):
    """SAP MARM. The only legal bridge between packaging and base units."""

    __tablename__ = "material_alternate_units"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    material_code: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    alt_uom: Mapped[str] = mapped_column(String(8), nullable=False)
    numerator: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    denominator: Mapped[Decimal] = mapped_column(QTY, nullable=False, default=Decimal(1))
    base_uom: Mapped[str] = mapped_column(String(8), nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "material_code", "alt_uom", name="uq_material_alt_uom"),
    )


class VendorModel(Base, TimestampMixin, TenantMixin):
    """Vendor master (SAP LFA1 + supplier performance attributes)."""

    __tablename__ = "vendors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    vendor_id: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    legal_name: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(40), default="ACTIVE", index=True)
    country: Mapped[str] = mapped_column(String(2), default="US", index=True)
    region: Mapped[str] = mapped_column(String(64), default="")
    city: Mapped[str] = mapped_column(String(120), default="")
    address_line1: Mapped[str] = mapped_column(String(255), default="")
    postal_code: Mapped[str] = mapped_column(String(20), default="")
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    default_incoterm: Mapped[str] = mapped_column(String(8), default="EXW")
    default_incoterm_location: Mapped[str] = mapped_column(String(120), default="")
    payment_terms: Mapped[str] = mapped_column(String(80), default="NET 30")
    tax_id: Mapped[str] = mapped_column(String(64), default="")
    duns_number: Mapped[str] = mapped_column(String(20), default="")
    email: Mapped[str] = mapped_column(String(255), default="", index=True)
    phone: Mapped[str] = mapped_column(String(64), default="")
    website: Mapped[str] = mapped_column(String(255), default="")
    # Qualification and performance
    qualified: Mapped[bool] = mapped_column(Boolean, default=True)
    qualification_expires_on: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    iso9001_certified: Mapped[bool] = mapped_column(Boolean, default=False)
    iso14001_certified: Mapped[bool] = mapped_column(Boolean, default=False)
    iatf16949_certified: Mapped[bool] = mapped_column(Boolean, default=False)
    certifications: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    capability_tags: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    on_time_delivery_pct: Mapped[Decimal] = mapped_column(PCT, default=Decimal(95))
    quality_ppm: Mapped[int] = mapped_column(Integer, default=500)
    quality_rejection_pct: Mapped[Decimal] = mapped_column(PCT, default=Decimal("1.0"))
    responsiveness_score: Mapped[Decimal] = mapped_column(SCORE, default=Decimal(70))
    average_quote_turnaround_days: Mapped[Decimal] = mapped_column(SCORE, default=Decimal(5))
    quote_response_rate_pct: Mapped[Decimal] = mapped_column(PCT, default=Decimal(70))
    financial_risk: Mapped[str] = mapped_column(String(16), default="LOW")
    geopolitical_risk: Mapped[str] = mapped_column(String(16), default="LOW")
    risk_notes: Mapped[str] = mapped_column(Text, default="")
    blocked_reason: Mapped[str] = mapped_column(Text, default="")
    spend_ytd_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    first_po_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_po_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    search_text: Mapped[str] = mapped_column(Text, default="")
    embedding: Mapped[list[float] | None] = mapped_column(
        EmbeddingVector(get_settings().embedding_dimensions), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "vendor_id", name="uq_vendor_id"),
        Index("idx_vendor_status_country", "tenant_id", "status", "country"),
        CheckConstraint("on_time_delivery_pct >= 0 AND on_time_delivery_pct <= 100", name="ck_vendor_otd"),
    )


class VendorContactModel(Base, TimestampMixin, TenantMixin):
    __tablename__ = "vendor_contacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    vendor_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(180), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    phone: Mapped[str] = mapped_column(String(64), default="")
    role: Mapped[str] = mapped_column(String(80), default="SALES")
    is_primary_rfq_contact: Mapped[bool] = mapped_column(Boolean, default=False)
    language: Mapped[str] = mapped_column(String(8), default="en")
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "vendor_id", "email", name="uq_vendor_contact_email"),
    )


class SourceListModel(Base, TimestampMixin, TenantMixin):
    """Approved vendor list per material/plant (SAP EORD)."""

    __tablename__ = "source_list"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    material_code: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    plant_code: Mapped[str] = mapped_column(String(8), nullable=False)
    vendor_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fixed_source: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    mrp_relevant: Mapped[bool] = mapped_column(Boolean, default=True)
    approval_reference: Mapped[str] = mapped_column(String(120), default="")

    __table_args__ = (
        Index("idx_source_list_lookup", "tenant_id", "material_code", "plant_code", "vendor_id"),
    )


class InfoRecordModel(Base, TimestampMixin, TenantMixin):
    """Purchasing info record (SAP EINA/EINE): the agreed material/vendor price.

    Maintaining this after negotiation is one of the explicit deliverables of
    the workflow, so it is a first-class table rather than a derived view.
    """

    __tablename__ = "info_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    info_record_number: Mapped[str] = mapped_column(String(40), nullable=False)
    material_code: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    vendor_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    plant_code: Mapped[str] = mapped_column(String(8), default="")
    purchasing_org: Mapped[str] = mapped_column(String(8), default="1000")
    net_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    price_unit: Mapped[int] = mapped_column(Integer, default=1)
    order_uom: Mapped[str] = mapped_column(String(8), default="EA")
    minimum_order_quantity: Mapped[Decimal] = mapped_column(QTY, default=Decimal(1))
    planned_delivery_days: Mapped[int] = mapped_column(Integer, default=14)
    incoterm: Mapped[str] = mapped_column(String(8), default="EXW")
    incoterm_location: Mapped[str] = mapped_column(String(120), default="")
    payment_terms: Mapped[str] = mapped_column(String(80), default="NET 30")
    tax_code: Mapped[str] = mapped_column(String(8), default="")
    price_scales: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_case_id: Mapped[str] = mapped_column(String(64), default="")
    source_quotation_id: Mapped[str] = mapped_column(String(36), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    superseded_by_id: Mapped[str] = mapped_column(String(36), default="")

    __table_args__ = (
        Index("idx_info_record_lookup", "tenant_id", "material_code", "vendor_id", "is_active"),
        Index("idx_info_record_validity", "tenant_id", "material_code", "valid_from"),
        UniqueConstraint("tenant_id", "info_record_number", name="uq_info_record_number"),
    )


class ContractModel(Base, TimestampMixin, TenantMixin):
    """Outline agreement / framework contract (SAP EKKO doc type MK/LP)."""

    __tablename__ = "contracts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    contract_number: Mapped[str] = mapped_column(String(40), nullable=False)
    vendor_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    contract_type: Mapped[str] = mapped_column(String(16), default="QUANTITY")
    description: Mapped[str] = mapped_column(String(500), default="")
    target_value: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    released_value: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payment_terms: Mapped[str] = mapped_column(String(80), default="NET 30")
    incoterm: Mapped[str] = mapped_column(String(8), default="EXW")
    price_protection_clause: Mapped[bool] = mapped_column(Boolean, default=False)
    materials: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (UniqueConstraint("tenant_id", "contract_number", name="uq_contract_number"),)


class PurchaseHistoryModel(Base):
    """Flat historical PO line mirror (SAP EKKO x EKPO extract).

    The largest table in the system. Deliberately foreign-key free and
    denormalised: this is an append-only mirror loaded by COPY, and the
    importer - not the database - guarantees consistency. Bitemporal columns
    (`valid_from`/`valid_to`) let an overlapping re-export supersede rows
    instead of destructively overwriting them.
    """

    __tablename__ = "purchase_history"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(36), nullable=False)
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    po_number: Mapped[str] = mapped_column(String(40), nullable=False)
    po_line: Mapped[str] = mapped_column(String(10), nullable=False)
    po_type: Mapped[str] = mapped_column(String(8), default="NB")
    material_code: Mapped[str] = mapped_column(String(40), nullable=False)
    material_description: Mapped[str] = mapped_column(String(500), default="")
    material_group: Mapped[str] = mapped_column(String(20), default="")
    plant_code: Mapped[str] = mapped_column(String(8), default="")
    purchasing_org: Mapped[str] = mapped_column(String(8), default="1000")
    purchasing_group: Mapped[str] = mapped_column(String(8), default="001")
    vendor_id: Mapped[str] = mapped_column(String(40), nullable=False)
    vendor_name: Mapped[str] = mapped_column(String(255), default="")
    quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    uom: Mapped[str] = mapped_column(String(8), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    price_unit: Mapped[int] = mapped_column(Integer, default=1)
    net_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    exchange_rate: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal(1))
    net_value_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    base_currency: Mapped[str] = mapped_column(String(3), default="USD")
    order_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivery_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_delivery_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    incoterm: Mapped[str] = mapped_column(String(8), default="")
    incoterm_location: Mapped[str] = mapped_column(String(120), default="")
    payment_terms: Mapped[str] = mapped_column(String(80), default="")
    delivered_quantity: Mapped[Decimal] = mapped_column(QTY, default=Decimal(0))
    invoiced_quantity: Mapped[Decimal] = mapped_column(QTY, default=Decimal(0))
    rejected_quantity: Mapped[Decimal] = mapped_column(QTY, default=Decimal(0))
    on_time: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    days_late: Mapped[int] = mapped_column(Integer, default=0)
    deletion_indicator: Mapped[bool] = mapped_column(Boolean, default=False)
    requisition_number: Mapped[str] = mapped_column(String(40), default="")
    contract_number: Mapped[str] = mapped_column(String(40), default="")
    info_record_number: Mapped[str] = mapped_column(String(40), default="")
    created_by: Mapped[str] = mapped_column(String(120), default="")
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    learned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "snapshot_id", "row_hash", name="uq_snapshot_row"),
        # The workhorse index: "what did we last pay for this material?"
        Index(
            "idx_ph_material_date",
            "tenant_id",
            "material_code",
            "order_date",
            postgresql_include=["vendor_id", "unit_price", "currency", "quantity", "uom"],
        ),
        Index("idx_ph_vendor_date", "tenant_id", "vendor_id", "order_date"),
        Index("idx_ph_material_vendor", "tenant_id", "material_code", "vendor_id", "order_date"),
        Index("idx_ph_group_date", "tenant_id", "material_group", "order_date"),
        Index("idx_ph_po", "tenant_id", "po_number"),
        Index("idx_ph_current", "tenant_id", "material_code", "valid_to"),
    )


class GoodsReceiptHistoryModel(Base):
    """Flat GR mirror (SAP MSEG). Feeds supplier delivery performance."""

    __tablename__ = "goods_receipt_history"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(36), nullable=False)
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    material_document: Mapped[str] = mapped_column(String(40), nullable=False)
    document_line: Mapped[str] = mapped_column(String(10), default="1")
    po_number: Mapped[str] = mapped_column(String(40), nullable=False)
    po_line: Mapped[str] = mapped_column(String(10), nullable=False)
    material_code: Mapped[str] = mapped_column(String(40), nullable=False)
    vendor_id: Mapped[str] = mapped_column(String(40), nullable=False)
    plant_code: Mapped[str] = mapped_column(String(8), default="")
    movement_type: Mapped[str] = mapped_column(String(8), default="101")
    quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    uom: Mapped[str] = mapped_column(String(8), nullable=False)
    posting_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scheduled_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    days_late: Mapped[int] = mapped_column(Integer, default=0)
    inspection_result: Mapped[str] = mapped_column(String(24), default="ACCEPTED")
    rejected_quantity: Mapped[Decimal] = mapped_column(QTY, default=Decimal(0))
    rejection_reason: Mapped[str] = mapped_column(String(255), default="")
    batch: Mapped[str] = mapped_column(String(40), default="")
    learned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "snapshot_id", "row_hash", name="uq_gr_snapshot_row"),
        Index("idx_gr_vendor_date", "tenant_id", "vendor_id", "posting_date"),
        Index("idx_gr_material_date", "tenant_id", "material_code", "posting_date"),
        Index("idx_gr_po", "tenant_id", "po_number", "po_line"),
    )


class FxRateModel(Base, TenantMixin):
    __tablename__ = "fx_rates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    quote_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    as_of: Mapped[datetime] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(40), default="ECB")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "base_currency", "quote_currency", "as_of", "source", name="uq_fx_rate"
        ),
        Index("idx_fx_lookup", "tenant_id", "base_currency", "quote_currency", "as_of"),
    )


class FreightRateModel(Base, TenantMixin, TimestampMixin):
    """Lane freight and duty reference used to normalise Incoterms."""

    __tablename__ = "freight_rates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    origin_country: Mapped[str] = mapped_column(String(2), nullable=False)
    destination_plant: Mapped[str] = mapped_column(String(8), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), default="SEA")
    cost_per_kg: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    cost_per_shipment: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    transit_days: Mapped[int] = mapped_column(Integer, default=30)
    customs_clearance_cost: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    insurance_pct: Mapped[Decimal] = mapped_column(PCT, default=Decimal("0.35"))
    duty_rate_pct: Mapped[Decimal] = mapped_column(PCT, default=Decimal(0))

    __table_args__ = (
        Index("idx_freight_lane", "tenant_id", "origin_country", "destination_plant", "mode"),
    )


# ═══════════════════════════════════════════════════════════════════════════
# 2. EVIDENCE KNOWLEDGE - immutable documents, chunks, claims
# ═══════════════════════════════════════════════════════════════════════════


class DocumentModel(Base, TimestampMixin, TenantMixin):
    """Logical document identity. Content lives in versions, never here."""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    logical_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    document_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    case_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    material_code: Mapped[str] = mapped_column(String(40), default="", index=True)
    vendor_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    current_version_id: Mapped[str] = mapped_column(String(36), default="")

    versions: Mapped[list[DocumentVersionModel]] = relationship(
        back_populates="document", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (Index("idx_document_scope", "tenant_id", "case_id", "document_type"),)


class DocumentVersionModel(Base, TenantMixin):
    """An immutable version. `content_hash` makes re-upload a no-op."""

    __tablename__ = "document_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    version_label: Mapped[str] = mapped_column(String(80), default="1")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    original_filename: Mapped[str] = mapped_column(String(500), default="")
    authority: Mapped[str] = mapped_column(String(80), default="UNKNOWN", index=True)
    trust_state: Mapped[str] = mapped_column(String(40), default="UNVERIFIED", index=True)
    extracted_text_uri: Mapped[str] = mapped_column(Text, default="")
    extracted_char_count: Mapped[int] = mapped_column(Integer, default=0)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    extraction_method: Mapped[str] = mapped_column(String(64), default="")
    firewall_verdict: Mapped[str] = mapped_column(String(40), default="PENDING")
    firewall_findings: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(SafeJsonb, default=dict)
    uploaded_by: Mapped[str] = mapped_column(String(120), default="")
    received_from: Mapped[str] = mapped_column(String(255), default="")
    supersedes_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    document: Mapped[DocumentModel] = relationship(back_populates="versions")

    __table_args__ = (
        UniqueConstraint("tenant_id", "content_hash", name="uq_docversion_content"),
        Index("idx_docversion_doc", "tenant_id", "document_id", "created_at"),
        Index("idx_docversion_trust", "tenant_id", "trust_state"),
    )


class DocumentChunkModel(Base, TenantMixin):
    """Retrievable text chunk with its embedding. The RAG surface."""

    __tablename__ = "document_chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    document_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document_versions.id", ondelete="CASCADE"), index=True
    )
    case_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0)
    page_number: Mapped[int] = mapped_column(Integer, default=0)
    section_path: Mapped[str] = mapped_column(String(500), default="")
    authority: Mapped[str] = mapped_column(String(80), default="UNKNOWN")
    trust_state: Mapped[str] = mapped_column(String(40), default="UNVERIFIED")
    embedding: Mapped[list[float] | None] = mapped_column(
        EmbeddingVector(get_settings().embedding_dimensions), nullable=True
    )
    embedding_model: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("document_version_id", "chunk_index", name="uq_chunk_index"),
        Index("idx_chunk_case", "tenant_id", "case_id"),
        Index("idx_chunk_trust", "tenant_id", "trust_state"),
    )


class ClaimModel(Base, TenantMixin):
    """An atomic subject-predicate-value assertion with full provenance.

    Every claim knows which document version it came from, who asserted it, how
    much it should be trusted, and what it superseded. This is what makes an
    evaluation defensible six months later.
    """

    __tablename__ = "claims"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    predicate: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    value_text: Mapped[str] = mapped_column(Text, default="")
    normalized_value: Mapped[str] = mapped_column(Text, default="")
    numeric_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    uom: Mapped[str] = mapped_column(String(16), default="")
    authority: Mapped[str] = mapped_column(String(80), default="UNKNOWN", index=True)
    trust_state: Mapped[str] = mapped_column(String(40), default="UNVERIFIED", index=True)
    confidence: Mapped[Decimal] = mapped_column(SCORE, default=Decimal(0))
    document_version_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    chunk_id: Mapped[str] = mapped_column(String(36), default="")
    source_location: Mapped[str] = mapped_column(String(255), default="")
    source_excerpt: Mapped[str] = mapped_column(Text, default="")
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    learned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    supersedes_claim_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    extracted_by: Mapped[str] = mapped_column(String(120), default="")

    __table_args__ = (
        UniqueConstraint("tenant_id", "content_hash", name="uq_claim_content"),
        Index("idx_claim_sp", "tenant_id", "subject", "predicate", "trust_state"),
        Index("idx_claim_case", "tenant_id", "case_id", "predicate"),
    )


class ClaimConflictModel(Base, TenantMixin, TimestampMixin):
    """Two claims about the same subject+predicate that disagree.

    Conflicts are surfaced, never silently resolved - the wrong pick here is how
    an agent ends up "confirming" a spec the supplier never actually met.
    """

    __tablename__ = "claim_conflicts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    predicate: Mapped[str] = mapped_column(String(255), nullable=False)
    claim_a_id: Mapped[str] = mapped_column(String(36), nullable=False)
    claim_b_id: Mapped[str] = mapped_column(String(36), nullable=False)
    conflict_type: Mapped[str] = mapped_column(String(64), default="VALUE_MISMATCH")
    severity: Mapped[str] = mapped_column(String(16), default="MEDIUM")
    detail: Mapped[str] = mapped_column(Text, default="")
    resolution: Mapped[str] = mapped_column(String(40), default="UNRESOLVED")
    resolved_by: Mapped[str] = mapped_column(String(120), default="")
    resolved_claim_id: Mapped[str] = mapped_column(String(36), default="")

    __table_args__ = (
        UniqueConstraint("tenant_id", "claim_a_id", "claim_b_id", name="uq_claim_conflict"),
    )


class SecurityFindingModel(Base, TenantMixin, TimestampMixin):
    """Document-firewall output. Never deleted; quarantine is auditable."""

    __tablename__ = "security_findings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    document_version_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    communication_id: Mapped[str] = mapped_column(String(36), default="")
    finding_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    matched_excerpt: Mapped[str] = mapped_column(Text, default="")
    disposition: Mapped[str] = mapped_column(String(64), default="")
    acknowledged_by: Mapped[str] = mapped_column(String(120), default="")
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (Index("idx_finding_case_sev", "tenant_id", "case_id", "severity"),)


# ═══════════════════════════════════════════════════════════════════════════
# 3. AGENT STATE - the durable case file
# ═══════════════════════════════════════════════════════════════════════════


class PurchaseRequisitionModel(Base, TenantMixin, TimestampMixin):
    __tablename__ = "purchase_requisitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    pr_number: Mapped[str] = mapped_column(String(40), nullable=False)
    plant_code: Mapped[str] = mapped_column(String(8), nullable=False)
    company_code: Mapped[str] = mapped_column(String(8), default="1000")
    requester: Mapped[str] = mapped_column(String(180), nullable=False)
    requester_email: Mapped[str] = mapped_column(String(255), default="")
    department: Mapped[str] = mapped_column(String(120), default="")
    priority: Mapped[str] = mapped_column(String(16), default="NORMAL")
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    justification: Mapped[str] = mapped_column(Text, default="")
    budget_code: Mapped[str] = mapped_column(String(64), default="")
    source_channel: Mapped[str] = mapped_column(String(32), default="API")
    raw_document_version_id: Mapped[str] = mapped_column(String(36), default="")
    parse_confidence: Mapped[Decimal] = mapped_column(SCORE, default=Decimal(0))
    parse_warnings: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    validation_errors: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    estimated_value_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))

    lines: Mapped[list[PurchaseRequisitionLineModel]] = relationship(
        back_populates="requisition", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (UniqueConstraint("tenant_id", "pr_number", name="uq_pr_number"),)


class PurchaseRequisitionLineModel(Base, TenantMixin):
    __tablename__ = "purchase_requisition_lines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    requisition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("purchase_requisitions.id", ondelete="CASCADE"), index=True
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    material_code: Mapped[str] = mapped_column(String(40), default="", index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    uom: Mapped[str] = mapped_column(String(8), nullable=False)
    normalized_uom: Mapped[str] = mapped_column(String(8), default="")
    required_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    plant_code: Mapped[str] = mapped_column(String(8), default="")
    storage_location: Mapped[str] = mapped_column(String(8), default="")
    cost_center: Mapped[str] = mapped_column(String(20), default="")
    gl_account: Mapped[str] = mapped_column(String(20), default="")
    estimated_unit_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="")
    specification_reference: Mapped[str] = mapped_column(String(200), default="")
    manufacturer_part_number: Mapped[str] = mapped_column(String(120), default="")
    preferred_vendor_id: Mapped[str] = mapped_column(String(40), default="")
    free_text_only: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str] = mapped_column(Text, default="")
    # Filled by material-master validation (stage 2)
    resolved_material_code: Mapped[str] = mapped_column(String(40), default="")
    resolution_method: Mapped[str] = mapped_column(String(40), default="")
    resolution_confidence: Mapped[Decimal] = mapped_column(SCORE, default=Decimal(0))
    validation_status: Mapped[str] = mapped_column(String(40), default="PENDING")
    validation_messages: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    requisition: Mapped[PurchaseRequisitionModel] = relationship(back_populates="lines")

    __table_args__ = (
        UniqueConstraint("requisition_id", "line_number", name="uq_pr_line_number"),
    )


class SourcingCaseModel(Base, TenantMixin, TimestampMixin):
    __tablename__ = "sourcing_cases"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    pr_number: Mapped[str] = mapped_column(String(40), nullable=False)
    requisition_id: Mapped[str] = mapped_column(String(36), default="")
    state: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), default="")
    plant_code: Mapped[str] = mapped_column(String(8), default="")
    buyer_id: Mapped[str] = mapped_column(String(120), default="", index=True)
    category: Mapped[str] = mapped_column(String(120), default="")
    commercial_unlocked: Mapped[bool] = mapped_column(Boolean, default=False)
    technical_approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    award_approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    awarded_supplier_id: Mapped[str] = mapped_column(String(40), default="")
    negotiation_round: Mapped[int] = mapped_column(SmallInteger, default=0)
    reminder_counts: Mapped[dict[str, Any]] = mapped_column(SafeJsonb, default=dict)
    estimated_value_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    awarded_value_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    savings_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    base_currency: Mapped[str] = mapped_column(String(3), default="USD")
    workflow_id: Mapped[str] = mapped_column(String(255), default="")
    workflow_run_id: Mapped[str] = mapped_column(String(255), default="")
    state_history: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    cancellation_reason: Mapped[str] = mapped_column(Text, default="")
    failure_reason: Mapped[str] = mapped_column(Text, default="")
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (
        UniqueConstraint("tenant_id", "pr_number", name="uq_case_pr_number"),
        Index("idx_case_state_updated", "tenant_id", "state", "updated_at"),
        Index("idx_case_buyer", "tenant_id", "buyer_id", "state"),
    )


class RequirementModel(Base, TenantMixin):
    __tablename__ = "requirements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    pr_line_number: Mapped[int] = mapped_column(Integer, default=1)
    requirement_key: Mapped[str] = mapped_column(String(80), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    obligation: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    attribute: Mapped[str] = mapped_column(String(255), nullable=False)
    operator: Mapped[str] = mapped_column(String(24), nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    target_value: Mapped[str] = mapped_column(Text, default="")
    target_numeric: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    lower_numeric: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    upper_numeric: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    tolerance_plus: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    tolerance_minus: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    uom: Mapped[str] = mapped_column(String(16), default="")
    allowed_values: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    weight: Mapped[Decimal] = mapped_column(SCORE, default=Decimal(1))
    source_document_version_id: Mapped[str] = mapped_column(String(36), default="")
    source_location: Mapped[str] = mapped_column(String(255), default="")
    trust_state: Mapped[str] = mapped_column(String(40), default="UNVERIFIED")
    extraction_confidence: Mapped[Decimal] = mapped_column(SCORE, default=Decimal(0))
    reviewed_by: Mapped[str] = mapped_column(String(120), default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "case_id", "requirement_key", name="uq_requirement_key"),
        Index("idx_requirement_case", "tenant_id", "case_id", "obligation", "active"),
    )


class SupplierCandidateModel(Base, TenantMixin, TimestampMixin):
    """A shortlisted supplier with its full, reproducible score breakdown."""

    __tablename__ = "supplier_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    vendor_id: Mapped[str] = mapped_column(String(40), nullable=False)
    vendor_name: Mapped[str] = mapped_column(String(255), default="")
    rank: Mapped[int] = mapped_column(Integer, default=0)
    total_score: Mapped[Decimal] = mapped_column(SCORE, default=Decimal(0))
    history_score: Mapped[Decimal] = mapped_column(SCORE, default=Decimal(0))
    performance_score: Mapped[Decimal] = mapped_column(SCORE, default=Decimal(0))
    capability_score: Mapped[Decimal] = mapped_column(SCORE, default=Decimal(0))
    commercial_score: Mapped[Decimal] = mapped_column(SCORE, default=Decimal(0))
    risk_score: Mapped[Decimal] = mapped_column(SCORE, default=Decimal(0))
    responsiveness_score: Mapped[Decimal] = mapped_column(SCORE, default=Decimal(0))
    similarity_score: Mapped[Decimal] = mapped_column(SCORE, default=Decimal(0))
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(SafeJsonb, default=dict)
    rationale: Mapped[str] = mapped_column(Text, default="")
    selection_source: Mapped[str] = mapped_column(String(40), default="SCORED")
    selected: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    excluded_reason: Mapped[str] = mapped_column(Text, default="")
    added_by: Mapped[str] = mapped_column(String(120), default="AGENT")
    last_purchase_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_unit_price_base: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    purchase_count_36m: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("tenant_id", "case_id", "vendor_id", name="uq_candidate_case_vendor"),
        Index("idx_candidate_selected", "tenant_id", "case_id", "selected", "rank"),
    )


class RfqModel(Base, TenantMixin, TimestampMixin):
    __tablename__ = "rfqs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    rfq_number: Mapped[str] = mapped_column(String(40), nullable=False)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(40), default="DRAFT", index=True)
    title: Mapped[str] = mapped_column(String(500), default="")
    issue_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    response_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    validity_days_required: Mapped[int] = mapped_column(Integer, default=60)
    delivery_plant: Mapped[str] = mapped_column(String(8), default="")
    delivery_address: Mapped[str] = mapped_column(Text, default="")
    required_incoterm: Mapped[str] = mapped_column(String(8), default="DAP")
    required_incoterm_location: Mapped[str] = mapped_column(String(120), default="")
    currency_preference: Mapped[str] = mapped_column(String(3), default="USD")
    payment_terms_target: Mapped[str] = mapped_column(String(80), default="NET 45")
    sealed_bid: Mapped[bool] = mapped_column(Boolean, default=True)
    terms_and_conditions: Mapped[str] = mapped_column(Text, default="")
    instructions: Mapped[str] = mapped_column(Text, default="")
    document_version_id: Mapped[str] = mapped_column(String(36), default="")
    response_token_salt: Mapped[str] = mapped_column(String(64), default="")
    released_by: Mapped[str] = mapped_column(String(120), default="")
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    lines: Mapped[list[RfqLineModel]] = relationship(
        back_populates="rfq", cascade="all, delete-orphan", lazy="selectin"
    )
    invitations: Mapped[list[RfqInvitationModel]] = relationship(
        back_populates="rfq", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "rfq_number", name="uq_rfq_number"),
        Index("idx_rfq_case_status", "tenant_id", "case_id", "status"),
    )


class RfqLineModel(Base, TenantMixin):
    __tablename__ = "rfq_lines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    rfq_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("rfqs.id", ondelete="CASCADE"), index=True
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    pr_line_number: Mapped[int] = mapped_column(Integer, default=1)
    material_code: Mapped[str] = mapped_column(String(40), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    uom: Mapped[str] = mapped_column(String(8), nullable=False)
    required_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    target_unit_price_base: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    should_cost_base: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    requirement_ids: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    quantity_breaks: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    rfq: Mapped[RfqModel] = relationship(back_populates="lines")

    __table_args__ = (UniqueConstraint("rfq_id", "line_number", name="uq_rfq_line_number"),)


class RfqInvitationModel(Base, TenantMixin, TimestampMixin):
    """One supplier's invitation, including its private response token."""

    __tablename__ = "rfq_invitations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    rfq_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("rfqs.id", ondelete="CASCADE"), index=True
    )
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    vendor_id: Mapped[str] = mapped_column(String(40), nullable=False)
    vendor_name: Mapped[str] = mapped_column(String(255), default="")
    contact_email: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_name: Mapped[str] = mapped_column(String(180), default="")
    status: Mapped[str] = mapped_column(String(40), default="DRAFT", index=True)
    response_token: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    reply_to_address: Mapped[str] = mapped_column(String(255), default="")
    thread_message_id: Mapped[str] = mapped_column(String(255), default="", index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_contact_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reminders_sent: Mapped[int] = mapped_column(SmallInteger, default=0)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    declined_reason: Mapped[str] = mapped_column(Text, default="")
    bounce_reason: Mapped[str] = mapped_column(Text, default="")

    rfq: Mapped[RfqModel] = relationship(back_populates="invitations")

    __table_args__ = (
        UniqueConstraint("rfq_id", "vendor_id", name="uq_invitation_vendor"),
        Index("idx_invitation_status", "tenant_id", "case_id", "status"),
    )


class QuotationModel(Base, TenantMixin, TimestampMixin):
    """A supplier's response.

    Commercial fields are nullable and may be held encrypted in
    `sealed_payload` until a human unlocks the commercial phase - that is the
    sealed-bid mechanism, enforced in the repository rather than by convention.
    """

    __tablename__ = "quotations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    quotation_number: Mapped[str] = mapped_column(String(60), default="")
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    rfq_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    invitation_id: Mapped[str] = mapped_column(String(36), default="")
    vendor_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    vendor_name: Mapped[str] = mapped_column(String(255), default="")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    negotiation_round: Mapped[int] = mapped_column(SmallInteger, default=0)
    status: Mapped[str] = mapped_column(String(40), default="RECEIVED", index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    received_via: Mapped[str] = mapped_column(String(32), default="EMAIL")
    source_communication_id: Mapped[str] = mapped_column(String(36), default="")
    document_version_id: Mapped[str] = mapped_column(String(36), default="")
    # Commercial header
    currency: Mapped[str] = mapped_column(String(3), default="")
    incoterm: Mapped[str] = mapped_column(String(8), default="")
    incoterm_location: Mapped[str] = mapped_column(String(120), default="")
    payment_terms: Mapped[str] = mapped_column(String(120), default="")
    validity_days: Mapped[int] = mapped_column(Integer, default=0)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lead_time_days: Mapped[int] = mapped_column(Integer, default=0)
    freight_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    packing_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    tooling_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    other_charges: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    discount_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    total_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    warranty_months: Mapped[int] = mapped_column(Integer, default=0)
    minimum_order_quantity: Mapped[Decimal | None] = mapped_column(QTY, nullable=True)
    # Sealed-bid storage
    is_sealed: Mapped[bool] = mapped_column(Boolean, default=False)
    sealed_payload: Mapped[str] = mapped_column(Text, default="")
    sealed_key_id: Mapped[str] = mapped_column(String(255), default="")
    unsealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unsealed_by: Mapped[str] = mapped_column(String(120), default="")
    # Parsing / evaluation
    parse_confidence: Mapped[Decimal] = mapped_column(SCORE, default=Decimal(0))
    parse_warnings: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    clarifications_requested: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    technical_score: Mapped[Decimal | None] = mapped_column(SCORE, nullable=True)
    technically_qualified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    disqualification_reasons: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    raw_extract: Mapped[dict[str, Any]] = mapped_column(SafeJsonb, default=dict)
    supersedes_quotation_id: Mapped[str] = mapped_column(String(36), default="")

    lines: Mapped[list[QuotationLineModel]] = relationship(
        back_populates="quotation", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        Index("idx_quotation_case_vendor", "tenant_id", "case_id", "vendor_id", "revision"),
        Index("idx_quotation_status", "tenant_id", "case_id", "status"),
    )


class QuotationLineModel(Base, TenantMixin):
    __tablename__ = "quotation_lines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    quotation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("quotations.id", ondelete="CASCADE"), index=True
    )
    rfq_line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    material_code: Mapped[str] = mapped_column(String(40), default="")
    offered_description: Mapped[str] = mapped_column(Text, default="")
    offered_part_number: Mapped[str] = mapped_column(String(120), default="")
    quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    uom: Mapped[str] = mapped_column(String(8), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    price_per_quantity: Mapped[Decimal] = mapped_column(QTY, default=Decimal(1))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    lead_time_days: Mapped[int] = mapped_column(Integer, default=0)
    minimum_order_quantity: Mapped[Decimal | None] = mapped_column(QTY, nullable=True)
    is_alternative: Mapped[bool] = mapped_column(Boolean, default=False)
    quantity_breaks: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    technical_attributes: Mapped[dict[str, Any]] = mapped_column(SafeJsonb, default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    quotation: Mapped[QuotationModel] = relationship(back_populates="lines")

    __table_args__ = (
        UniqueConstraint(
            "quotation_id", "rfq_line_number", "is_alternative", name="uq_quotation_line"
        ),
    )


class ComplianceAssessmentModel(Base, TenantMixin, TimestampMixin):
    """The technical comparison matrix, one cell per requirement x supplier."""

    __tablename__ = "compliance_assessments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    quotation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    requirement_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    vendor_id: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    offered_value: Mapped[str] = mapped_column(Text, default="")
    offered_numeric: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    offered_uom: Mapped[str] = mapped_column(String(16), default="")
    rationale: Mapped[str] = mapped_column(Text, default="")
    evidence_ids: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    confidence: Mapped[Decimal] = mapped_column(SCORE, default=Decimal(0))
    assessed_by: Mapped[str] = mapped_column(String(64), default="AGENT")
    deviation_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    deviation_approval_id: Mapped[str] = mapped_column(String(36), default="")
    reviewer_override_status: Mapped[str] = mapped_column(String(24), default="")
    reviewer_id: Mapped[str] = mapped_column(String(120), default="")
    reviewer_note: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (
        UniqueConstraint("quotation_id", "requirement_id", name="uq_assessment_cell"),
        Index("idx_assessment_case_status", "tenant_id", "case_id", "status"),
    )


class NormalizedOfferModel(Base, TenantMixin, TimestampMixin):
    """Commercially comparable form of one quotation line.

    Every adjustment is stored separately so a buyer can see exactly why the
    headline price and the landed cost differ.
    """

    __tablename__ = "normalized_offers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    quotation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    quotation_line_id: Mapped[str] = mapped_column(String(36), default="")
    vendor_id: Mapped[str] = mapped_column(String(40), nullable=False)
    rfq_line_number: Mapped[int] = mapped_column(Integer, default=1)
    negotiation_round: Mapped[int] = mapped_column(SmallInteger, default=0)
    quantity_base_uom: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    base_uom: Mapped[str] = mapped_column(String(8), nullable=False)
    quoted_unit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    quoted_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    fx_rate: Mapped[Decimal] = mapped_column(Numeric(20, 10), default=Decimal(1))
    fx_as_of: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    unit_price_base: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    ext_price_base: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    freight_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    insurance_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    duty_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    customs_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    packing_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    tooling_amortized_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    other_charges_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    discount_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    landed_cost_base: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    payment_terms_raw: Mapped[str] = mapped_column(String(120), default="")
    payment_terms_net_days: Mapped[int] = mapped_column(Integer, default=30)
    payment_terms_adjustment_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    lead_time_days: Mapped[int] = mapped_column(Integer, default=0)
    lead_time_penalty_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    quality_risk_adjustment_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    total_cost_of_ownership_base: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    normalized_unit_cost_base: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    base_currency: Mapped[str] = mapped_column(String(3), default="USD")
    incoterm_from: Mapped[str] = mapped_column(String(8), default="")
    incoterm_to: Mapped[str] = mapped_column(String(8), default="DAP")
    adjustments: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    assumptions: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    warnings: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    comparable: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (
        Index("idx_normalized_case_round", "tenant_id", "case_id", "negotiation_round"),
        UniqueConstraint(
            "quotation_id", "quotation_line_id", "negotiation_round", name="uq_normalized_line"
        ),
    )


class BidRankingModel(Base, TenantMixin, TimestampMixin):
    """L1/L2/L3 position for one supplier in one ranking run."""

    __tablename__ = "bid_rankings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ranking_run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    negotiation_round: Mapped[int] = mapped_column(SmallInteger, default=0)
    basis: Mapped[str] = mapped_column(String(32), default="TCO")
    vendor_id: Mapped[str] = mapped_column(String(40), nullable=False)
    vendor_name: Mapped[str] = mapped_column(String(255), default="")
    quotation_id: Mapped[str] = mapped_column(String(36), default="")
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    position_label: Mapped[str] = mapped_column(String(8), default="")
    total_base: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    landed_cost_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    tco_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    delta_vs_l1_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    delta_vs_l1_pct: Mapped[Decimal] = mapped_column(PCT, default=Decimal(0))
    delta_vs_benchmark_pct: Mapped[Decimal | None] = mapped_column(PCT, nullable=True)
    technical_score: Mapped[Decimal | None] = mapped_column(SCORE, nullable=True)
    weighted_value_score: Mapped[Decimal | None] = mapped_column(SCORE, nullable=True)
    technically_qualified: Mapped[bool] = mapped_column(Boolean, default=True)
    lines_covered: Mapped[int] = mapped_column(Integer, default=0)
    lines_total: Mapped[int] = mapped_column(Integer, default=0)
    partial_offer: Mapped[bool] = mapped_column(Boolean, default=False)
    flags: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    notes: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (
        UniqueConstraint("ranking_run_id", "vendor_id", name="uq_ranking_vendor"),
        Index("idx_ranking_case_position", "tenant_id", "case_id", "position"),
    )


class NegotiationRoundModel(Base, TenantMixin, TimestampMixin):
    """One immutable negotiation round.

    Rounds are versioned rather than mutated: the full chain of what was asked,
    what came back and what it saved is reconstructable for audit.
    """

    __tablename__ = "negotiation_rounds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    round_number: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="DRAFT", index=True)
    strategy: Mapped[str] = mapped_column(String(64), default="TARGET_PRICE")
    rationale: Mapped[str] = mapped_column(Text, default="")
    baseline_ranking_run_id: Mapped[str] = mapped_column(String(36), default="")
    baseline_total_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    target_total_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    achieved_total_base: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    savings_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    savings_pct: Mapped[Decimal] = mapped_column(PCT, default=Decimal(0))
    approval_id: Mapped[str] = mapped_column(String(36), default="")
    opened_by: Mapped[str] = mapped_column(String(120), default="")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    targets: Mapped[list[NegotiationTargetModel]] = relationship(
        back_populates="round", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "case_id", "round_number", name="uq_negotiation_round"),
    )


class NegotiationTargetModel(Base, TenantMixin, TimestampMixin):
    """Per-supplier ask within a round, and what they came back with."""

    __tablename__ = "negotiation_targets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    round_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("negotiation_rounds.id", ondelete="CASCADE"), index=True
    )
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    vendor_id: Mapped[str] = mapped_column(String(40), nullable=False)
    vendor_name: Mapped[str] = mapped_column(String(255), default="")
    baseline_quotation_id: Mapped[str] = mapped_column(String(36), default="")
    response_quotation_id: Mapped[str] = mapped_column(String(36), default="")
    current_total_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    target_total_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    target_reduction_pct: Mapped[Decimal] = mapped_column(PCT, default=Decimal(0))
    achieved_total_base: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    achieved_reduction_pct: Mapped[Decimal | None] = mapped_column(PCT, nullable=True)
    non_price_asks: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    leverage_points: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    message_body: Mapped[str] = mapped_column(Text, default="")
    communication_id: Mapped[str] = mapped_column(String(36), default="")
    status: Mapped[str] = mapped_column(String(32), default="DRAFT")
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    round: Mapped[NegotiationRoundModel] = relationship(back_populates="targets")

    __table_args__ = (UniqueConstraint("round_id", "vendor_id", name="uq_negotiation_target"),)


class PoRecommendationModel(Base, TenantMixin, TimestampMixin):
    """The final deliverable: a draft PO awaiting human release into SAP."""

    __tablename__ = "po_recommendations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    recommendation_number: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="RECOMMENDED", index=True)
    vendor_id: Mapped[str] = mapped_column(String(40), nullable=False)
    vendor_name: Mapped[str] = mapped_column(String(255), default="")
    quotation_id: Mapped[str] = mapped_column(String(36), default="")
    ranking_run_id: Mapped[str] = mapped_column(String(36), default="")
    plant_code: Mapped[str] = mapped_column(String(8), default="")
    purchasing_org: Mapped[str] = mapped_column(String(8), default="1000")
    purchasing_group: Mapped[str] = mapped_column(String(8), default="001")
    document_type: Mapped[str] = mapped_column(String(8), default="NB")
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    incoterm: Mapped[str] = mapped_column(String(8), default="DAP")
    incoterm_location: Mapped[str] = mapped_column(String(120), default="")
    payment_terms: Mapped[str] = mapped_column(String(120), default="NET 45")
    total_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    total_amount_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    benchmark_total_base: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    savings_vs_benchmark_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    savings_vs_first_offer_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    justification: Mapped[str] = mapped_column(Text, default="")
    award_rationale: Mapped[dict[str, Any]] = mapped_column(SafeJsonb, default=dict)
    approval_chain: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    approval_chain_satisfied: Mapped[bool] = mapped_column(Boolean, default=False)
    sap_payload: Mapped[dict[str, Any]] = mapped_column(SafeJsonb, default=dict)
    document_version_id: Mapped[str] = mapped_column(String(36), default="")
    released_by: Mapped[str] = mapped_column(String(120), default="")
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    erp_po_number: Mapped[str] = mapped_column(String(40), default="")
    expected_delivery_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    lines: Mapped[list[PoRecommendationLineModel]] = relationship(
        back_populates="recommendation", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "recommendation_number", name="uq_po_rec_number"),
    )


class PoRecommendationLineModel(Base, TenantMixin):
    __tablename__ = "po_recommendation_lines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    recommendation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("po_recommendations.id", ondelete="CASCADE"), index=True
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    material_code: Mapped[str] = mapped_column(String(40), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    uom: Mapped[str] = mapped_column(String(8), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    price_unit: Mapped[int] = mapped_column(Integer, default=1)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    delivery_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    plant_code: Mapped[str] = mapped_column(String(8), default="")
    storage_location: Mapped[str] = mapped_column(String(8), default="")
    cost_center: Mapped[str] = mapped_column(String(20), default="")
    gl_account: Mapped[str] = mapped_column(String(20), default="")
    tax_code: Mapped[str] = mapped_column(String(8), default="")
    info_record_number: Mapped[str] = mapped_column(String(40), default="")
    benchmark_unit_price_base: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    price_variance_pct: Mapped[Decimal | None] = mapped_column(PCT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    recommendation: Mapped[PoRecommendationModel] = relationship(back_populates="lines")

    __table_args__ = (
        UniqueConstraint("recommendation_id", "line_number", name="uq_po_rec_line"),
    )


class InfoRecordProposalModel(Base, TenantMixin, TimestampMixin):
    """Proposed info-record create/update, generated from the awarded quote."""

    __tablename__ = "info_record_proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    material_code: Mapped[str] = mapped_column(String(40), nullable=False)
    vendor_id: Mapped[str] = mapped_column(String(40), nullable=False)
    plant_code: Mapped[str] = mapped_column(String(8), default="")
    action: Mapped[str] = mapped_column(String(16), default="CREATE")
    existing_info_record_id: Mapped[str] = mapped_column(String(36), default="")
    net_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    price_unit: Mapped[int] = mapped_column(Integer, default=1)
    order_uom: Mapped[str] = mapped_column(String(8), default="EA")
    minimum_order_quantity: Mapped[Decimal] = mapped_column(QTY, default=Decimal(1))
    planned_delivery_days: Mapped[int] = mapped_column(Integer, default=14)
    incoterm: Mapped[str] = mapped_column(String(8), default="DAP")
    payment_terms: Mapped[str] = mapped_column(String(80), default="NET 45")
    price_scales: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    previous_net_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    price_change_pct: Mapped[Decimal | None] = mapped_column(PCT, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="PROPOSED", index=True)
    applied_by: Mapped[str] = mapped_column(String(120), default="")
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resulting_info_record_id: Mapped[str] = mapped_column(String(36), default="")

    __table_args__ = (
        Index("idx_info_proposal_case", "tenant_id", "case_id", "status"),
    )


class ApprovalModel(Base, TenantMixin):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    approval_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    actor_roles: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    subject_ref: Mapped[str] = mapped_column(String(120), default="")
    conditions: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    payload: Mapped[dict[str, Any]] = mapped_column(SafeJsonb, default=dict)
    signature: Mapped[str] = mapped_column(String(64), default="")
    ip_address: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("idx_approval_case_type", "tenant_id", "case_id", "approval_type", "created_at"),
    )


class CommunicationModel(Base, TenantMixin, TimestampMixin):
    """Every message in or out, with the raw body retained for audit."""

    __tablename__ = "communications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    rfq_id: Mapped[str] = mapped_column(String(36), default="")
    invitation_id: Mapped[str] = mapped_column(String(36), default="")
    vendor_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    communication_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="DRAFT", index=True)
    from_address: Mapped[str] = mapped_column(String(255), default="")
    to_addresses: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    cc_addresses: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    reply_to: Mapped[str] = mapped_column(String(255), default="")
    subject: Mapped[str] = mapped_column(Text, default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    body_html: Mapped[str] = mapped_column(Text, default="")
    body_hash: Mapped[str] = mapped_column(String(64), default="")
    external_message_id: Mapped[str] = mapped_column(String(998), default="", index=True)
    in_reply_to: Mapped[str] = mapped_column(String(998), default="")
    thread_token: Mapped[str] = mapped_column(String(64), default="", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), default="")
    attachment_refs: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    provider: Mapped[str] = mapped_column(String(32), default="")
    provider_message_id: Mapped[str] = mapped_column(String(255), default="")
    error_detail: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requires_release_by: Mapped[str] = mapped_column(String(120), default="")
    released_by: Mapped[str] = mapped_column(String(120), default="")
    storage_uri: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_communication_idempotency"),
        Index("idx_comm_case_type", "tenant_id", "case_id", "communication_type", "created_at"),
        Index("idx_comm_status", "tenant_id", "status", "direction"),
    )


class DecisionModel(Base, TenantMixin):
    """An agent recommendation, with the evidence that produced it."""

    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    decision_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, default=1)
    recommendation: Mapped[dict[str, Any]] = mapped_column(SafeJsonb, default=dict)
    rationale: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[Decimal] = mapped_column(SCORE, default=Decimal(0))
    model_metadata: Mapped[dict[str, Any]] = mapped_column(SafeJsonb, default=dict)
    input_digest: Mapped[str] = mapped_column(String(64), default="")
    accepted: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    accepted_by: Mapped[str] = mapped_column(String(120), default="")
    override_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("idx_decision_case_type", "tenant_id", "case_id", "decision_type", "created_at"),
    )


class DecisionEvidenceModel(Base):
    __tablename__ = "decision_evidence"

    decision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("decisions.id", ondelete="CASCADE"), primary_key=True
    )
    evidence_type: Mapped[str] = mapped_column(String(48), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    evidence_version: Mapped[str] = mapped_column(String(120), primary_key=True, default="")
    role: Mapped[str] = mapped_column(String(32), default="SUPPORTS")
    excerpt: Mapped[str] = mapped_column(Text, default="")
    weight: Mapped[Decimal] = mapped_column(SCORE, default=Decimal(1))


class ScheduledReminderModel(Base, TenantMixin, TimestampMixin):
    """Delivery-follow-up schedule. Temporal owns the timers; this is the
    queryable projection buyers actually look at."""

    __tablename__ = "scheduled_reminders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    reminder_type: Mapped[str] = mapped_column(String(48), nullable=False)
    subject_ref: Mapped[str] = mapped_column(String(120), default="")
    vendor_id: Mapped[str] = mapped_column(String(40), default="")
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt: Mapped[int] = mapped_column(SmallInteger, default=0)
    max_attempts: Mapped[int] = mapped_column(SmallInteger, default=3)
    status: Mapped[str] = mapped_column(String(24), default="SCHEDULED", index=True)
    escalation_level: Mapped[int] = mapped_column(SmallInteger, default=0)
    payload: Mapped[dict[str, Any]] = mapped_column(SafeJsonb, default=dict)

    __table_args__ = (Index("idx_reminder_due", "tenant_id", "status", "due_at"),)


class AuditLogModel(Base, TenantMixin):
    """Append-only audit trail. Every state change and every human action."""

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(120), default="")
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    actor_id: Mapped[str] = mapped_column(String(120), default="SYSTEM", index=True)
    actor_type: Mapped[str] = mapped_column(String(24), default="SYSTEM")
    actor_roles: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    before_state: Mapped[dict[str, Any]] = mapped_column(SafeJsonb, default=dict)
    after_state: Mapped[dict[str, Any]] = mapped_column(SafeJsonb, default=dict)
    detail: Mapped[str] = mapped_column(Text, default="")
    correlation_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    workflow_id: Mapped[str] = mapped_column(String(255), default="")
    ip_address: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    __table_args__ = (
        Index("idx_audit_case_time", "tenant_id", "case_id", "created_at"),
        Index("idx_audit_entity", "tenant_id", "entity_type", "entity_id"),
    )


class IdempotencyKeyModel(Base, TenantMixin):
    """Guards non-idempotent side effects across Temporal activity retries."""

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    result_ref: Mapped[str] = mapped_column(String(255), default="")
    result_payload: Mapped[dict[str, Any]] = mapped_column(SafeJsonb, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("idx_idempotency_scope", "tenant_id", "scope", "created_at"),)


class UserModel(Base, TenantMixin, TimestampMixin):
    """Local identity projection. In production SSO is authoritative; this
    caches role assignment and approval delegation."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    actor_id: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(180), default="")
    roles: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    department: Mapped[str] = mapped_column(String(120), default="")
    plant_scope: Mapped[list[Any]] = mapped_column(SafeJsonb, default=list)
    approval_limit_base: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    external_subject: Mapped[str] = mapped_column(String(255), default="", index=True)
    api_key_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "actor_id", name="uq_user_actor"),
        UniqueConstraint("tenant_id", "email", name="uq_user_email"),
    )


# Tables whose embedding columns get an ANN index when the cluster supports it.
VECTOR_INDEXED_TABLES: tuple[tuple[str, str, str], ...] = (
    ("document_chunks", "embedding", "idx_chunk_embedding_ann"),
    ("materials", "embedding", "idx_material_embedding_ann"),
    ("vendors", "embedding", "idx_vendor_embedding_ann"),
)

# Highest-volume tables; the seed loader uses COPY for these.
BULK_LOAD_TABLES: tuple[str, ...] = (
    "purchase_history",
    "goods_receipt_history",
    "materials",
    "material_plants",
    "vendors",
    "info_records",
)
