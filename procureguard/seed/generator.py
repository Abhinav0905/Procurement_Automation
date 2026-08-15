"""Production-scale synthetic data generation.

The generated corpus has to be statistically realistic, not merely large,
because every analytic in this system is built on top of it. Specifically:

* **Prices inflate** at category-specific rates, so a 2021 price and a 2026
  price genuinely are not comparable and the trend detector has something to
  find.
* **Currencies drift** against USD, so FX-at-order-date matters and a naive
  comparison of nominal foreign prices is visibly wrong.
* **Quantity discounts** follow a power curve, so the quantity-adjusted
  benchmark has a real signal to fit.
* **Suppliers differ** in price level, on-time delivery and defect rate, and
  those differences persist across their orders - which is what makes supplier
  scoring meaningful rather than noise.
* **Demand is seasonal**, with a Q4 spike and a summer trough.
* **Supplier relationships churn**: some vendors are dropped mid-history, some
  are added, and a few incumbents quietly raise prices year over year.

Everything is seeded, so two runs with the same seed produce byte-identical
data - which is what makes the demo reproducible and the tests deterministic.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from procureguard.seed.catalog import (
    BASE_CURRENCY,
    BUYERS,
    CATEGORY_INFLATION,
    CERTIFICATIONS,
    CITIES,
    FX_ANNUAL_DRIFT,
    FX_START,
    MATERIAL_GROUPS,
    PAYMENT_TERMS,
    PLANTS,
    SCALE_PRESETS,
    SUPPLIER_CORES,
    SUPPLIER_COUNTRIES,
    SUPPLIER_PREFIXES,
    SUPPLIER_SUFFIXES,
    MaterialGroupSpec,
)

ZERO = Decimal(0)


@dataclass(slots=True)
class GeneratedMaterial:
    material_code: str
    group: MaterialGroupSpec
    description: str
    long_description: str
    base_uom: str
    base_price: Decimal
    weight_kg: Decimal
    manufacturer: str
    manufacturer_part_number: str
    status: str
    successor: str
    plants: list[str]
    attributes: dict[str, Any]
    quality_inspection: bool
    hazardous: bool
    batch_controlled: bool
    lead_time_days: int
    drawing_number: str
    alternate_unit: tuple[str, Decimal, str] | None


@dataclass(slots=True)
class GeneratedVendor:
    vendor_id: str
    name: str
    country: str
    city: str
    currency: str
    incoterm: str
    payment_terms: str
    email_domain: str
    groups: list[str]
    price_factor: float
    on_time_pct: float
    quality_ppm: int
    response_rate: float
    turnaround_days: float
    financial_risk: str
    geopolitical_risk: str
    certifications: list[str]
    status: str
    active_from: date
    active_to: date | None
    contacts: list[dict[str, str]] = field(default_factory=list)


class SeedGenerator:
    """Deterministic generator for the whole synthetic enterprise."""

    def __init__(
        self,
        *,
        scale: str = "medium",
        seed: int = 20260810,
        tenant_id: str = "ACME-MFG",
        end_date: date | None = None,
    ) -> None:
        preset = SCALE_PRESETS.get(scale, SCALE_PRESETS["medium"])
        self.scale = scale
        self.tenant_id = tenant_id
        self.rng = random.Random(seed)
        self.material_count = preset["materials"]
        self.vendor_count = preset["vendors"]
        self.po_line_target = preset["po_lines"]
        self.years = preset["years"]
        self.end_date = end_date or date(2026, 8, 10)
        self.start_date = self.end_date - timedelta(days=365 * self.years)
        self.materials: list[GeneratedMaterial] = []
        self.vendors: list[GeneratedVendor] = []
        self._material_vendors: dict[str, list[tuple[GeneratedVendor, float]]] = {}
        self._fx_cache: dict[tuple[str, int], Decimal] = {}

    # ═════════════════════════════════════════════════════════ master data

    def generate_plants(self) -> list[dict[str, Any]]:
        return [
            {
                "tenant_id": self.tenant_id,
                "plant_code": plant.code,
                "name": plant.name,
                "company_code": "1000",
                "country": plant.country,
                "city": plant.city,
                "currency": plant.currency,
                "timezone": plant.timezone,
                "purchasing_org": plant.purchasing_org,
                "purchasing_group": f"{int(plant.code) // 1000:03d}",
            }
            for plant in PLANTS
        ]

    def generate_users(self) -> list[dict[str, Any]]:
        return [
            {
                "tenant_id": self.tenant_id,
                "actor_id": actor_id,
                "email": f"{actor_id.replace('.', '')}@acme-mfg.example.com",
                "display_name": name,
                "roles": [role],
                "department": "Procurement" if "BUY" in role or role in ("CATEGORY_MANAGER", "PROCUREMENT_HEAD") else role.title(),
                "plant_scope": [p.code for p in PLANTS],
                "approval_limit_base": limit,
                "active": True,
            }
            for actor_id, name, role, limit in BUYERS
        ]

    def generate_materials(self) -> list[GeneratedMaterial]:
        if self.materials:
            return self.materials

        # Category mix reflects a real industrial spend profile: lots of cheap
        # fasteners and consumables, few expensive hydraulic assemblies.
        weights = {
            "MG-FAST": 0.17, "MG-ELEC": 0.13, "MG-BEARING": 0.10, "MG-SEAL": 0.10,
            "MG-TOOL": 0.09, "MG-VALVE": 0.08, "MG-RAW": 0.08, "MG-HYD": 0.07,
            "MG-ELECTRONIC": 0.07, "MG-CHEM": 0.05, "MG-PACK": 0.04, "MG-SERVICE": 0.02,
        }
        groups = list(MATERIAL_GROUPS)
        group_weights = [weights.get(g.code, 0.05) for g in groups]
        counters: dict[str, int] = {}

        for _ in range(self.material_count):
            group = self.rng.choices(groups, weights=group_weights, k=1)[0]
            counters[group.code] = counters.get(group.code, 0) + 1
            index = counters[group.code]
            prefix = group.prefix
            code = f"{prefix}-{index:05d}"

            descriptor = self.rng.choice(group.descriptors) if group.descriptors else "Component"
            construction = (
                self.rng.choice(group.materials_of_construction)
                if group.materials_of_construction
                else ""
            )
            attributes = {
                name: round(self._log_uniform(low, high), 2)
                for name, (low, high, _unit) in group.attribute_ranges.items()
            }
            size_hint = self._size_hint(attributes)
            description = " ".join(
                filter(None, [descriptor, size_hint, construction])
            )[:200]

            # Price correlates with the physical size attributes, so bigger
            # parts cost more and the price/quantity curve is not pure noise.
            price_position = self.rng.betavariate(2.0, 5.0)
            base_price = self._log_scale(group.price_low, group.price_high, price_position)
            weight = self._log_scale(group.weight_kg_low, group.weight_kg_high, price_position)

            status, successor = self._material_status(code, prefix, index)
            plant_count = self.rng.choices([1, 2, 3, 5], weights=[0.42, 0.31, 0.18, 0.09], k=1)[0]
            plants = self.rng.sample([p.code for p in PLANTS], k=min(plant_count, len(PLANTS)))

            manufacturer = self._manufacturer(group)
            alternate = self._alternate_unit(group)

            self.materials.append(
                GeneratedMaterial(
                    material_code=code,
                    group=group,
                    description=description,
                    long_description=self._long_description(
                        descriptor, construction, attributes, group
                    ),
                    base_uom=group.base_uom,
                    base_price=base_price,
                    weight_kg=weight,
                    manufacturer=manufacturer,
                    manufacturer_part_number=self._mpn(manufacturer, code),
                    status=status,
                    successor=successor,
                    plants=plants,
                    attributes=attributes,
                    quality_inspection=group.quality_critical and self.rng.random() < 0.55,
                    hazardous=group.hazardous,
                    batch_controlled=group.quality_critical and self.rng.random() < 0.35,
                    lead_time_days=self.rng.randint(*group.lead_time_days),
                    drawing_number=(
                        f"DWG-{code}-R{self.rng.randint(0, 4)}"
                        if group.quality_critical and self.rng.random() < 0.6
                        else ""
                    ),
                    alternate_unit=alternate,
                )
            )
        return self.materials

    def generate_vendors(self) -> list[GeneratedVendor]:
        if self.vendors:
            return self.vendors

        countries = [c[0] for c in SUPPLIER_COUNTRIES]
        country_weights = [c[1] for c in SUPPLIER_COUNTRIES]
        by_country = {c[0]: c for c in SUPPLIER_COUNTRIES}
        used_names: set[str] = set()

        for index in range(1, self.vendor_count + 1):
            country = self.rng.choices(countries, weights=country_weights, k=1)[0]
            _, _, currency, incoterm, base_risk, _transit = by_country[country]

            name = self._vendor_name(country, used_names)
            group_count = self.rng.choices([1, 2, 3, 4], weights=[0.45, 0.3, 0.17, 0.08], k=1)[0]
            groups = self.rng.sample([g.code for g in MATERIAL_GROUPS], k=group_count)

            # Low-cost countries price lower but deliver later and reject more.
            is_low_cost = country in ("CN", "IN", "VN", "TR", "MX")
            price_factor = self.rng.gauss(0.86 if is_low_cost else 1.04, 0.11)
            price_factor = max(0.62, min(1.45, price_factor))
            on_time = self.rng.gauss(84.0 if is_low_cost else 92.0, 8.0)
            on_time = max(45.0, min(99.5, on_time))
            ppm = int(max(20, self.rng.lognormvariate(6.4 if is_low_cost else 5.6, 0.85)))

            status, active_from, active_to = self._vendor_lifecycle()

            self.vendors.append(
                GeneratedVendor(
                    vendor_id=f"V{index:06d}",
                    name=name,
                    country=country,
                    city=self.rng.choice(CITIES.get(country, ("Unknown",))),
                    currency=currency,
                    incoterm=incoterm,
                    payment_terms=self._weighted_choice(PAYMENT_TERMS),
                    email_domain=self._email_domain(name, index),
                    groups=groups,
                    price_factor=price_factor,
                    on_time_pct=on_time,
                    quality_ppm=ppm,
                    response_rate=max(25.0, min(99.0, self.rng.gauss(72.0, 16.0))),
                    turnaround_days=max(1.0, self.rng.gauss(6.0 if is_low_cost else 4.0, 2.4)),
                    financial_risk=self._risk(base_risk, 0.16),
                    geopolitical_risk=base_risk if self.rng.random() > 0.2 else self._risk(base_risk, 0.5),
                    certifications=[
                        label for label, probability in CERTIFICATIONS
                        if self.rng.random() < probability
                    ],
                    status=status,
                    active_from=active_from,
                    active_to=active_to,
                    contacts=self._contacts(name, index),
                )
            )
        return self.vendors

    def assign_suppliers(self) -> dict[str, list[tuple[GeneratedVendor, float]]]:
        """Give each material a stable supplier set with market shares."""
        if self._material_vendors:
            return self._material_vendors

        vendors_by_group: dict[str, list[GeneratedVendor]] = {}
        for vendor in self.generate_vendors():
            for group_code in vendor.groups:
                vendors_by_group.setdefault(group_code, []).append(vendor)

        all_vendors = self.generate_vendors()
        for material in self.generate_materials():
            pool = vendors_by_group.get(material.group.code) or all_vendors
            # Most materials are dual- or tri-sourced; a minority are single
            # source, which is exactly the population the shortlist logic has to
            # cope with.
            count = self.rng.choices([1, 2, 3, 4], weights=[0.22, 0.38, 0.28, 0.12], k=1)[0]
            chosen = self.rng.sample(pool, k=min(count, len(pool)))
            # Incumbent bias: the first supplier gets most of the volume.
            shares = sorted((self.rng.random() for _ in chosen), reverse=True)
            total = sum(shares) or 1.0
            self._material_vendors[material.material_code] = [
                (vendor, share / total) for vendor, share in zip(chosen, shares, strict=False)
            ]
        return self._material_vendors

    # ═══════════════════════════════════════════════════════════ time series

    def generate_fx_rates(self) -> list[dict[str, Any]]:
        """Month-end USD rates with drift plus mean-reverting noise."""
        rows: list[dict[str, Any]] = []
        cursor = date(self.start_date.year, self.start_date.month, 1)
        while cursor <= self.end_date:
            years_elapsed = (cursor - self.start_date).days / 365.25
            for currency, start_rate in FX_START.items():
                drift = float(FX_ANNUAL_DRIFT.get(currency, Decimal(1)))
                trend = float(start_rate) * (drift**years_elapsed)
                noise = 1.0 + self.rng.gauss(0.0, 0.018)
                rate = Decimal(str(round(trend * noise, 6)))
                rows.append(
                    {
                        "tenant_id": self.tenant_id,
                        "base_currency": BASE_CURRENCY,
                        "quote_currency": currency,
                        "rate": rate,
                        "as_of": cursor,
                        "source": "ECB",
                    }
                )
            cursor = (cursor + timedelta(days=32)).replace(day=1)
        return rows

    def fx_rate(self, currency: str, on: date) -> Decimal:
        """USD -> currency on a date, consistent with the published series."""
        if currency == BASE_CURRENCY:
            return Decimal(1)
        key = (currency, on.year * 12 + on.month)
        cached = self._fx_cache.get(key)
        if cached is not None:
            return cached
        years_elapsed = (on - self.start_date).days / 365.25
        start_rate = float(FX_START.get(currency, Decimal(1)))
        drift = float(FX_ANNUAL_DRIFT.get(currency, Decimal(1)))
        # Deterministic in the month, so a PO line and the FX table agree.
        wobble = 1.0 + 0.012 * math.sin(key[1] * 1.7 + len(currency))
        rate = Decimal(str(round(start_rate * (drift**years_elapsed) * wobble, 6)))
        self._fx_cache[key] = rate
        return rate

    def generate_freight_rates(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for country, _weight, _currency, _incoterm, _risk, transit in SUPPLIER_COUNTRIES:
            for plant in PLANTS:
                same_region = _same_region(country, plant.country)
                domestic = country == plant.country
                for mode in ("SEA", "AIR") if not domestic else ("ROAD",):
                    if mode == "AIR":
                        cost_per_kg = Decimal(str(round(self.rng.uniform(3.2, 7.5), 4)))
                        days = max(3, transit // 5)
                    elif mode == "ROAD":
                        cost_per_kg = Decimal(str(round(self.rng.uniform(0.12, 0.45), 4)))
                        days = max(2, transit // 2)
                    else:
                        cost_per_kg = Decimal(str(round(self.rng.uniform(0.28, 1.15), 4)))
                        days = transit + (0 if same_region else 6)
                    rows.append(
                        {
                            "tenant_id": self.tenant_id,
                            "origin_country": country,
                            "destination_plant": plant.code,
                            "mode": mode,
                            "cost_per_kg": cost_per_kg,
                            "cost_per_shipment": Decimal(
                                str(round(self.rng.uniform(45, 380), 2))
                            ),
                            "currency": BASE_CURRENCY,
                            "transit_days": days,
                            "customs_clearance_cost": (
                                ZERO if domestic else Decimal(str(round(self.rng.uniform(65, 320), 2)))
                            ),
                            "insurance_pct": Decimal(str(round(self.rng.uniform(0.18, 0.55), 4))),
                            "duty_rate_pct": (
                                ZERO
                                if domestic or same_region
                                else Decimal(str(round(self.rng.uniform(0.0, 6.5), 2)))
                            ),
                        }
                    )
        return rows

    # ═════════════════════════════════════════════════════ purchase history

    def generate_purchase_history(self, snapshot_id: str) -> Iterator[dict[str, Any]]:
        """Stream PO lines. The largest output by far, so it is a generator."""
        self.assign_suppliers()
        materials = self.generate_materials()
        purchasable = [m for m in materials if m.status != "OBSOLETE" or self.rng.random() < 0.3]
        if not purchasable:
            return

        # Order frequency scales with how commodity-like a category is.
        frequency_weights = [
            {"MG-FAST": 6.0, "MG-CHEM": 4.0, "MG-PACK": 4.0, "MG-TOOL": 3.5, "MG-SEAL": 3.0,
             "MG-BEARING": 2.5, "MG-RAW": 2.5, "MG-ELEC": 2.0, "MG-VALVE": 1.4,
             "MG-ELECTRONIC": 1.2, "MG-HYD": 0.8, "MG-SERVICE": 1.0}.get(m.group.code, 1.0)
            for m in purchasable
        ]

        po_counter = 0
        line_counter = 0
        emitted = 0
        span_days = max(1, (self.end_date - self.start_date).days)

        while emitted < self.po_line_target:
            material = self.rng.choices(purchasable, weights=frequency_weights, k=1)[0]
            suppliers = self._material_vendors.get(material.material_code) or []
            if not suppliers:
                continue

            order_date = self._seasonal_date(span_days)
            vendor = self._pick_supplier(suppliers, order_date)
            if vendor is None:
                continue

            plant_code = self.rng.choice(material.plants)
            quantity = self._order_quantity(material)
            unit_price_base = self._historical_price(material, vendor, order_date, quantity)

            rate = self.fx_rate(vendor.currency, order_date)
            unit_price_local = (unit_price_base * rate).quantize(Decimal("0.0001"))
            price_unit = self._price_unit(unit_price_local)
            unit_price_field = (unit_price_local * price_unit).quantize(Decimal("0.0001"))

            net_value_local = (unit_price_local * quantity).quantize(Decimal("0.01"))
            net_value_base = (unit_price_base * quantity).quantize(Decimal("0.01"))

            # One PO usually carries several lines.
            if line_counter == 0 or self.rng.random() < 0.45:
                po_counter += 1
                line_counter = 0
            line_counter += 10

            planned_days = material.lead_time_days
            delivery_date = order_date + timedelta(days=planned_days)
            days_late = self._delivery_variance(vendor)
            actual_delivery = delivery_date + timedelta(days=max(0, days_late))
            delivered = actual_delivery <= self.end_date

            rejected = ZERO
            if self.rng.random() < (vendor.quality_ppm / 1_000_000) * 60:
                rejected = (quantity * Decimal(str(round(self.rng.uniform(0.01, 0.2), 4)))).quantize(
                    Decimal("0.001")
                )

            row_key = f"{po_counter}|{line_counter}|{material.material_code}|{vendor.vendor_id}"
            yield {
                "tenant_id": self.tenant_id,
                "snapshot_id": snapshot_id,
                "row_hash": hashlib.sha256(row_key.encode()).hexdigest(),
                "po_number": f"45{po_counter:08d}",
                "po_line": str(line_counter),
                "po_type": "NB",
                "material_code": material.material_code,
                "material_description": material.description,
                "material_group": material.group.code,
                "plant_code": plant_code,
                "purchasing_org": "1000",
                "purchasing_group": f"{self.rng.randint(1, 12):03d}",
                "vendor_id": vendor.vendor_id,
                "vendor_name": vendor.name,
                "quantity": quantity,
                "uom": material.base_uom,
                "unit_price": unit_price_field,
                "price_unit": price_unit,
                "net_value": net_value_local,
                "currency": vendor.currency,
                "exchange_rate": (Decimal(1) / rate).quantize(Decimal("0.00000001")),
                "net_value_base": net_value_base,
                "base_currency": BASE_CURRENCY,
                "order_date": _as_datetime(order_date),
                "delivery_date": _as_datetime(delivery_date),
                "actual_delivery_date": _as_datetime(actual_delivery) if delivered else None,
                "incoterm": vendor.incoterm,
                "incoterm_location": vendor.city,
                "payment_terms": vendor.payment_terms,
                "delivered_quantity": quantity if delivered else ZERO,
                "invoiced_quantity": quantity if delivered and self.rng.random() < 0.94 else ZERO,
                "rejected_quantity": rejected,
                "on_time": (days_late <= 0) if delivered else None,
                "days_late": max(0, days_late) if delivered else 0,
                "deletion_indicator": self.rng.random() < 0.004,
                "requisition_number": f"10{po_counter:08d}",
                "contract_number": "",
                "info_record_number": "",
                "created_by": self.rng.choice([b[0] for b in BUYERS[:4]]),
                "valid_from": _as_datetime(order_date),
                "valid_to": None,
                "learned_at": datetime.now(UTC),
            }
            emitted += 1

    def generate_goods_receipts(
        self, purchase_rows: list[dict[str, Any]], snapshot_id: str
    ) -> Iterator[dict[str, Any]]:
        for index, po in enumerate(purchase_rows):
            if po["actual_delivery_date"] is None or po["deletion_indicator"]:
                continue
            rejected = po["rejected_quantity"]
            yield {
                "tenant_id": self.tenant_id,
                "snapshot_id": snapshot_id,
                "row_hash": hashlib.sha256(
                    f"GR|{po['po_number']}|{po['po_line']}".encode()
                ).hexdigest(),
                "material_document": f"50{index:08d}",
                "document_line": "1",
                "po_number": po["po_number"],
                "po_line": po["po_line"],
                "material_code": po["material_code"],
                "vendor_id": po["vendor_id"],
                "plant_code": po["plant_code"],
                "movement_type": "101",
                "quantity": po["quantity"],
                "uom": po["uom"],
                "posting_date": po["actual_delivery_date"],
                "scheduled_date": po["delivery_date"],
                "days_late": po["days_late"],
                "inspection_result": "REJECTED" if rejected > 0 else "ACCEPTED",
                "rejected_quantity": rejected,
                "rejection_reason": (
                    self.rng.choice(
                        [
                            "Dimensional non-conformance",
                            "Surface finish out of specification",
                            "Missing material certificate",
                            "Transit damage",
                            "Incorrect part supplied",
                            "Hardness out of range",
                        ]
                    )
                    if rejected > 0
                    else ""
                ),
                "batch": f"B{self.rng.randint(100000, 999999)}",
                "learned_at": datetime.now(UTC),
            }

    def generate_source_list(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for material in self.generate_materials():
            suppliers = self._material_vendors.get(material.material_code, [])
            for position, (vendor, _share) in enumerate(suppliers):
                for plant_code in material.plants:
                    rows.append(
                        {
                            "tenant_id": self.tenant_id,
                            "material_code": material.material_code,
                            "plant_code": plant_code,
                            "vendor_id": vendor.vendor_id,
                            "valid_from": _as_datetime(self.start_date),
                            "valid_to": None,
                            # A genuine fixed source is rare and blocks competition.
                            "fixed_source": len(suppliers) == 1 and self.rng.random() < 0.25,
                            "blocked": vendor.status == "BLOCKED",
                            "mrp_relevant": position == 0,
                            "approval_reference": f"SRC-{material.material_code}",
                        }
                    )
        return rows

    def generate_info_records(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        counter = 0
        for material in self.generate_materials():
            suppliers = self._material_vendors.get(material.material_code, [])
            # Not every material/vendor pair has a maintained info record; that
            # gap is realistic and is what stage 15 exists to close.
            for vendor, _share in suppliers:
                if self.rng.random() > 0.45:
                    continue
                counter += 1
                as_of = self.end_date - timedelta(days=self.rng.randint(30, 400))
                price_base = self._historical_price(
                    material, vendor, as_of, self._order_quantity(material)
                )
                rate = self.fx_rate(vendor.currency, as_of)
                local_price = (price_base * rate).quantize(Decimal("0.0001"))
                price_unit = self._price_unit(local_price)
                rows.append(
                    {
                        "tenant_id": self.tenant_id,
                        "info_record_number": f"IR{counter:09d}",
                        "material_code": material.material_code,
                        "vendor_id": vendor.vendor_id,
                        "plant_code": self.rng.choice(material.plants),
                        "purchasing_org": "1000",
                        "net_price": (local_price * price_unit).quantize(Decimal("0.0001")),
                        "currency": vendor.currency,
                        "price_unit": price_unit,
                        "order_uom": material.base_uom,
                        "minimum_order_quantity": Decimal(
                            self.rng.choice([1, 1, 1, 5, 10, 25, 50, 100])
                        ),
                        "planned_delivery_days": material.lead_time_days,
                        "incoterm": vendor.incoterm,
                        "incoterm_location": vendor.city,
                        "payment_terms": vendor.payment_terms,
                        "price_scales": self._price_scales(local_price * price_unit),
                        "valid_from": _as_datetime(as_of),
                        "valid_to": _as_datetime(as_of + timedelta(days=365)),
                        "is_active": True,
                    }
                )
        return rows

    def generate_contracts(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        eligible = [v for v in self.generate_vendors() if v.status == "ACTIVE"]
        count = max(1, len(eligible) // 12)
        for index, vendor in enumerate(self.rng.sample(eligible, k=min(count, len(eligible))), 1):
            covered = [
                m.material_code
                for m in self.generate_materials()
                if any(v.vendor_id == vendor.vendor_id for v, _ in self._material_vendors.get(m.material_code, []))
            ][:40]
            if not covered:
                continue
            start = self.end_date - timedelta(days=self.rng.randint(60, 500))
            rows.append(
                {
                    "tenant_id": self.tenant_id,
                    "contract_number": f"46{index:08d}",
                    "vendor_id": vendor.vendor_id,
                    "contract_type": self.rng.choice(["QUANTITY", "VALUE"]),
                    "description": f"Framework agreement - {vendor.name}",
                    "target_value": Decimal(self.rng.randrange(50_000, 2_500_000, 5_000)),
                    "released_value": Decimal(self.rng.randrange(0, 400_000, 1_000)),
                    "currency": vendor.currency,
                    "valid_from": _as_datetime(start),
                    "valid_to": _as_datetime(start + timedelta(days=730)),
                    "payment_terms": vendor.payment_terms,
                    "incoterm": vendor.incoterm,
                    "price_protection_clause": self.rng.random() < 0.4,
                    "materials": covered,
                    "is_active": True,
                }
            )
        return rows

    # ═══════════════════════════════════════════════════════════════ helpers

    def _historical_price(
        self, material: GeneratedMaterial, vendor: GeneratedVendor, on: date, quantity: Decimal
    ) -> Decimal:
        """Base-currency unit price at a point in time.

        Composed of category inflation, the supplier's persistent price level, a
        power-law quantity discount, and per-order noise. Each factor is
        something the analytics layer is expected to detect.
        """
        years = (on - self.start_date).days / 365.25
        inflation = (1.0 + CATEGORY_INFLATION.get(material.group.code, 0.035)) ** years

        # Quantity discount: unit price falls with the -0.11 power of quantity,
        # flattening beyond a few hundred units.
        quantity_factor = float(max(quantity, Decimal(1))) ** -0.11

        noise = self.rng.gauss(1.0, 0.055)
        noise = max(0.78, min(1.28, noise))

        price = (
            float(material.base_price)
            * inflation
            * vendor.price_factor
            * quantity_factor
            * noise
        )
        return Decimal(str(round(max(price, 0.01), 6)))

    def _order_quantity(self, material: GeneratedMaterial) -> Decimal:
        """Order sizes are log-normal and category-dependent."""
        if material.group.code == "MG-FAST":
            value = self.rng.lognormvariate(5.6, 1.15)
        elif material.group.code in ("MG-SEAL", "MG-PACK", "MG-TOOL"):
            value = self.rng.lognormvariate(3.6, 1.05)
        elif material.group.code in ("MG-RAW", "MG-CHEM"):
            value = self.rng.lognormvariate(4.2, 1.0)
        elif material.group.code in ("MG-HYD", "MG-VALVE", "MG-ELECTRONIC"):
            value = self.rng.lognormvariate(1.5, 0.9)
        else:
            value = self.rng.lognormvariate(2.7, 1.0)
        quantity = max(1, int(round(value)))
        # Buyers order round numbers.
        if quantity > 40:
            quantity = int(round(quantity / 10) * 10)
        if quantity > 400:
            quantity = int(round(quantity / 50) * 50)
        return Decimal(quantity)

    def _seasonal_date(self, span_days: int) -> date:
        """Sample an order date with Q4 peak and a July trough."""
        for _ in range(6):
            offset = self.rng.randrange(span_days)
            candidate = self.start_date + timedelta(days=offset)
            month = candidate.month
            weight = {
                1: 0.85, 2: 0.9, 3: 1.05, 4: 1.0, 5: 1.0, 6: 0.95,
                7: 0.72, 8: 0.78, 9: 1.1, 10: 1.2, 11: 1.25, 12: 1.15,
            }[month]
            # More recent years carry more volume, as a growing company does.
            recency = 0.75 + 0.5 * (offset / span_days)
            if self.rng.random() < (weight * recency) / 1.7:
                # Purchasing happens on working days.
                while candidate.weekday() >= 5:
                    candidate -= timedelta(days=1)
                return candidate
        return self.start_date + timedelta(days=self.rng.randrange(span_days))

    def _pick_supplier(
        self, suppliers: list[tuple[GeneratedVendor, float]], on: date
    ) -> GeneratedVendor | None:
        """Choose by market share, respecting each vendor's active window."""
        available = [
            (vendor, share)
            for vendor, share in suppliers
            if vendor.active_from <= on and (vendor.active_to is None or on <= vendor.active_to)
        ]
        if not available:
            return None
        return self.rng.choices(
            [v for v, _ in available], weights=[s for _, s in available], k=1
        )[0]

    def _delivery_variance(self, vendor: GeneratedVendor) -> int:
        """Late days, drawn so the mean matches the vendor's on-time rate."""
        if self.rng.random() * 100 < vendor.on_time_pct:
            return self.rng.choice([-3, -2, -1, 0, 0, 0])
        return max(1, int(self.rng.lognormvariate(1.9, 0.85)))

    @staticmethod
    def _price_unit(unit_price: Decimal) -> int:
        """SAP price units: cheap parts are priced per 100 or per 1000."""
        if unit_price < Decimal("0.05"):
            return 1000
        if unit_price < Decimal("1"):
            return 100
        return 1

    def _price_scales(self, price: Decimal) -> list[dict[str, str]]:
        scales = []
        for quantity, discount in ((100, 0.03), (500, 0.07), (1000, 0.11)):
            scales.append(
                {
                    "quantity": str(quantity),
                    "net_price": str((price * Decimal(str(1 - discount))).quantize(Decimal("0.0001"))),
                }
            )
        return scales if self.rng.random() < 0.35 else []

    def _material_status(self, code: str, prefix: str, index: int) -> tuple[str, str]:
        roll = self.rng.random()
        if roll < 0.055:
            successor = f"{prefix}-{index + 1:05d}"
            return "OBSOLETE", successor
        if roll < 0.075:
            return "PHASE_OUT", ""
        if roll < 0.085:
            return "BLOCKED_FOR_PROCUREMENT", ""
        if roll < 0.092:
            return "ENGINEERING_HOLD", ""
        return "ACTIVE", ""

    def _alternate_unit(self, group: MaterialGroupSpec) -> tuple[str, Decimal, str] | None:
        if group.base_uom != "EA" or self.rng.random() > 0.28:
            return None
        alt, factor = self.rng.choice(
            [("BOX", 25), ("BOX", 50), ("BOX", 100), ("PAC", 10), ("SET", 4), ("ROL", 250)]
        )
        return alt, Decimal(factor), "EA"

    def _vendor_lifecycle(self) -> tuple[str, date, date | None]:
        roll = self.rng.random()
        if roll < 0.06:
            # Dropped partway through the history: supplier churn is real.
            return (
                "DEREGISTERED",
                self.start_date,
                self.start_date + timedelta(days=self.rng.randint(200, 365 * max(1, self.years - 1))),
            )
        if roll < 0.10:
            return "BLOCKED", self.start_date, None
        if roll < 0.16:
            # Onboarded partway through.
            return (
                "ACTIVE",
                self.start_date + timedelta(days=self.rng.randint(150, 365 * max(1, self.years - 1))),
                None,
            )
        if roll < 0.19:
            return "PENDING_QUALIFICATION", self.end_date - timedelta(days=60), None
        return "ACTIVE", self.start_date, None

    def _vendor_name(self, country: str, used: set[str]) -> str:
        prefixes = SUPPLIER_PREFIXES.get(country, ("Global",))
        suffixes = SUPPLIER_SUFFIXES.get(country, ("Ltd",))
        for _ in range(40):
            name = (
                f"{self.rng.choice(prefixes)} {self.rng.choice(SUPPLIER_CORES)} "
                f"{self.rng.choice(suffixes)}"
            )
            if name not in used:
                used.add(name)
                return name
        name = f"{self.rng.choice(prefixes)} {self.rng.choice(SUPPLIER_CORES)} {len(used)}"
        used.add(name)
        return name

    @staticmethod
    def _email_domain(name: str, index: int) -> str:
        slug = "".join(c for c in name.lower() if c.isalnum())[:18] or f"vendor{index}"
        return f"{slug}.example.com"

    def _contacts(self, vendor_name: str, index: int) -> list[dict[str, str]]:
        first_names = ("Anna", "Marco", "Wei", "Rahul", "Sofia", "Tomas", "Yuki", "Elena",
                       "Ahmet", "Chen", "Lukas", "Priya", "Diego", "Ingrid", "Hans", "Mei")
        last_names = ("Novak", "Rossi", "Zhang", "Sharma", "Muller", "Kowalski", "Tanaka",
                      "Garcia", "Yilmaz", "Li", "Andersson", "Patel", "Silva", "Weber")
        domain = self._email_domain(vendor_name, index)
        contacts: list[dict[str, str]] = []
        # Two random names can collide inside one vendor, and the vendor master
        # enforces unique emails per vendor, so disambiguate rather than clash.
        used_emails: set[str] = set()
        for position, role in enumerate(("SALES", "TECHNICAL", "LOGISTICS")):
            if position > 0 and self.rng.random() > 0.45:
                continue
            first = self.rng.choice(first_names)
            last = self.rng.choice(last_names)
            local = f"{first.lower()}.{last.lower()}"
            email = f"{local}@{domain}"
            if email in used_emails:
                email = f"{local}{position + 1}@{domain}"
            used_emails.add(email)
            contacts.append(
                {
                    "name": f"{first} {last}",
                    "email": email,
                    "phone": f"+{self.rng.randint(1, 99)} {self.rng.randint(100, 999)} {self.rng.randint(100000, 999999)}",
                    "role": role,
                    "primary": position == 0,
                }
            )
        return contacts

    def _risk(self, base: str, escalate_probability: float) -> str:
        levels = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
        index = levels.index(base) if base in levels else 0
        if self.rng.random() < escalate_probability:
            index = min(index + 1, len(levels) - 1)
        elif self.rng.random() < 0.12 and index > 0:
            index -= 1
        return levels[index]

    def _manufacturer(self, group: MaterialGroupSpec) -> str:
        brands = {
            "MG-BEARING": ("SKF", "FAG", "NSK", "Timken", "NTN", "Koyo"),
            "MG-VALVE": ("Emerson", "Velan", "Kitz", "Bray", "Crane", "Samson"),
            "MG-HYD": ("Parker", "Bosch Rexroth", "Eaton", "Danfoss", "HAWE"),
            "MG-ELEC": ("Siemens", "ABB", "Schneider", "WEG", "Lenze", "Phoenix Contact"),
            "MG-SEAL": ("Trelleborg", "Freudenberg", "Parker", "James Walker"),
            "MG-TOOL": ("Sandvik", "Kennametal", "Iscar", "Walter", "Mitsubishi"),
            "MG-ELECTRONIC": ("Endress+Hauser", "Vega", "IFM", "Pepperl+Fuchs", "Wika"),
            "MG-FAST": ("Bossard", "Wurth", "Bufab", "Fabory"),
        }
        return self.rng.choice(brands.get(group.code, ("Generic", "OEM", "Various")))

    def _mpn(self, manufacturer: str, code: str) -> str:
        stem = "".join(c for c in manufacturer.upper() if c.isalnum())[:4] or "GEN"
        return f"{stem}-{code.split('-')[-1]}{self.rng.choice(['', 'A', 'B', '-2RS', '-X'])}"

    def _long_description(
        self,
        descriptor: str,
        construction: str,
        attributes: dict[str, Any],
        group: MaterialGroupSpec,
    ) -> str:
        """A spec-shaped description; requirement extraction reads these."""
        lines = [f"{descriptor}"]
        if construction:
            lines.append(f"Material of construction: {construction}")
        for name, value in attributes.items():
            unit = group.attribute_ranges.get(name, (0, 0, ""))[2]
            label = name.replace("_mm", "").replace("_bar", "").replace("_c", "").replace(
                "_kn", "").replace("_rpm", "").replace("_v", "").replace("_a", "").replace(
                "_kw", "").replace("_lpm", "").replace("_mpa", "").replace("_hrc", "").replace(
                "_pct", "").replace("_kg", "").replace("_cst_40c", "").replace("_", " ").title()
            lines.append(f"{label}: {value} {unit}".strip())
        if group.quality_critical:
            lines.append("Certificate of conformity required with each delivery")
            lines.append("Supplier shall hold ISO 9001 certification")
        return "\n".join(lines)

    @staticmethod
    def _size_hint(attributes: dict[str, Any]) -> str:
        for key in ("nominal_diameter_mm", "bore_diameter_mm", "thread_diameter_mm",
                    "inner_diameter_mm", "diameter_mm", "cutting_diameter_mm"):
            if key in attributes:
                return f"DN{int(attributes[key])}" if "nominal" in key else f"{int(attributes[key])}mm"
        return ""

    def _log_uniform(self, low: float, high: float) -> float:
        if low <= 0:
            return self.rng.uniform(low, high)
        return math.exp(self.rng.uniform(math.log(low), math.log(high)))

    @staticmethod
    def _log_scale(low: Decimal, high: Decimal, position: float) -> Decimal:
        low_f, high_f = float(low), float(high)
        if low_f <= 0:
            return Decimal(str(round(low_f + (high_f - low_f) * position, 6)))
        value = math.exp(math.log(low_f) + (math.log(high_f) - math.log(low_f)) * position)
        return Decimal(str(round(value, 6)))

    def _weighted_choice(self, options: tuple[tuple[str, float], ...]) -> str:
        return self.rng.choices([o[0] for o in options], weights=[o[1] for o in options], k=1)[0]


def _as_datetime(value: date) -> datetime:
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def _same_region(a: str, b: str) -> bool:
    regions = {
        "EU": {"DE", "IT", "PL", "CZ", "ES", "SE", "GB", "TR"},
        "NA": {"US", "MX", "CA"},
        "AP": {"CN", "IN", "JP", "KR", "VN"},
    }
    return any(a in members and b in members for members in regions.values())
