"""Enterprise data repositories over the SAP mirror.

Everything here answers a bounded question with an indexed query. The model
never sees a table; it sees a small typed result. That is the whole point of the
data-boundary ADR: a million PO lines stay in CockroachDB, and twelve rows of
price history reach the prompt.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import case, desc, func, literal, or_, select, text

from procureguard.domain.money import FxRate, FxRateTable
from procureguard.domain.units import AlternateUnit
from procureguard.observability import logger

from .models import (
    ContractModel,
    FreightRateModel,
    FxRateModel,
    GoodsReceiptHistoryModel,
    InfoRecordModel,
    MaterialAlternateUnitModel,
    MaterialModel,
    MaterialPlantModel,
    PlantModel,
    PurchaseHistoryModel,
    SourceListModel,
    VendorContactModel,
    VendorModel,
    utcnow,
)
from .repo_core import TenantScopedRepository
from .vector import VectorSearch

log = logger(__name__)

PH = PurchaseHistoryModel


class SqlMaterialRepository(TenantScopedRepository):
    def get(self, material_code: str) -> MaterialModel | None:
        return self.session.scalars(
            self._scoped(select(MaterialModel), MaterialModel).where(
                MaterialModel.material_code == material_code.strip()
            )
        ).first()

    def get_many(self, codes: Sequence[str]) -> dict[str, MaterialModel]:
        if not codes:
            return {}
        rows = self.session.scalars(
            self._scoped(select(MaterialModel), MaterialModel).where(
                MaterialModel.material_code.in_(list({c.strip() for c in codes}))
            )
        ).all()
        return {row.material_code: row for row in rows}

    def get_case_insensitive(self, material_code: str) -> MaterialModel | None:
        """Buyers type 'val-1023'; SAP stores 'VAL-1023'."""
        return self.session.scalars(
            self._scoped(select(MaterialModel), MaterialModel).where(
                func.upper(MaterialModel.material_code) == material_code.strip().upper()
            )
        ).first()

    def find_by_manufacturer_part_number(self, mpn: str) -> list[MaterialModel]:
        if not mpn.strip():
            return []
        normalized = _strip_punctuation(mpn)
        return list(
            self.session.scalars(
                self._scoped(select(MaterialModel), MaterialModel)
                .where(
                    or_(
                        func.upper(MaterialModel.manufacturer_part_number) == mpn.strip().upper(),
                        func.upper(MaterialModel.manufacturer_part_number) == normalized,
                    )
                )
                .limit(20)
            ).all()
        )

    def search_text(self, query: str, *, limit: int = 20) -> list[MaterialModel]:
        """Token search over the denormalised search_text column.

        Ranking is "how many of the caller's tokens matched", computed in SQL so
        a broad query does not drag the whole candidate set back over the wire.
        """
        tokens = [t for t in _tokenize(query) if len(t) >= 3][:6]
        if not tokens:
            return []
        conditions = [MaterialModel.search_text.ilike(f"%{token}%") for token in tokens]
        match_count = sum(
            (case((condition, 1), else_=0) for condition in conditions),
            literal(0),
        ).label("match_count")
        rows = self.session.execute(
            self._scoped(select(MaterialModel, match_count), MaterialModel)
            .where(or_(*conditions))
            .order_by(desc("match_count"), MaterialModel.material_code)
            .limit(limit)
        ).all()
        return [row[0] for row in rows]

    def search_by_group(self, material_group: str, *, limit: int = 100) -> list[MaterialModel]:
        return list(
            self.session.scalars(
                self._scoped(select(MaterialModel), MaterialModel)
                .where(MaterialModel.material_group == material_group)
                .limit(limit)
            ).all()
        )

    def semantic_search(
        self, query_vector: Sequence[float], *, top_k: int = 10, dimensions: int = 1024
    ) -> list[tuple[str, float]]:
        searcher = VectorSearch(dimensions=dimensions, candidate_limit=2_000)
        hits = searcher.search(
            self.session.connection(),
            table="materials",
            id_column="material_code",
            embedding_column="embedding",
            query_vector=query_vector,
            top_k=top_k,
            where_sql="tenant_id = :tenant_id AND embedding IS NOT NULL",
            params={"tenant_id": self.tenant_id},
        )
        return [(hit.row_id, hit.score) for hit in hits]

    def get_plant_extension(self, material_code: str, plant_code: str) -> MaterialPlantModel | None:
        return self.session.scalars(
            self._scoped(select(MaterialPlantModel), MaterialPlantModel).where(
                MaterialPlantModel.material_code == material_code,
                MaterialPlantModel.plant_code == plant_code,
            )
        ).first()

    def list_plants_for_material(self, material_code: str) -> list[str]:
        rows = self.session.scalars(
            self._scoped(select(MaterialPlantModel.plant_code), MaterialPlantModel).where(
                MaterialPlantModel.material_code == material_code
            )
        ).all()
        return sorted(set(rows))

    def get_alternate_units(self, material_code: str) -> dict[str, AlternateUnit]:
        rows = self.session.scalars(
            self._scoped(select(MaterialAlternateUnitModel), MaterialAlternateUnitModel).where(
                MaterialAlternateUnitModel.material_code == material_code
            )
        ).all()
        out: dict[str, AlternateUnit] = {}
        for row in rows:
            denominator = Decimal(str(row.denominator or 1)) or Decimal(1)
            out[row.alt_uom] = AlternateUnit(
                alt_uom=row.alt_uom,
                factor=Decimal(str(row.numerator)) / denominator,
                base_uom=row.base_uom,
            )
        return out

    def get_plant(self, plant_code: str) -> PlantModel | None:
        return self.session.scalars(
            self._scoped(select(PlantModel), PlantModel).where(PlantModel.plant_code == plant_code)
        ).first()

    def count(self) -> int:
        return int(
            self.session.scalar(
                self._scoped(select(func.count()).select_from(MaterialModel), MaterialModel)
            )
            or 0
        )


class SqlVendorRepository(TenantScopedRepository):
    def get(self, vendor_id: str) -> VendorModel | None:
        return self.session.scalars(
            self._scoped(select(VendorModel), VendorModel).where(
                VendorModel.vendor_id == vendor_id.strip()
            )
        ).first()

    def get_many(self, vendor_ids: Sequence[str]) -> dict[str, VendorModel]:
        if not vendor_ids:
            return {}
        rows = self.session.scalars(
            self._scoped(select(VendorModel), VendorModel).where(
                VendorModel.vendor_id.in_(list(set(vendor_ids)))
            )
        ).all()
        return {row.vendor_id: row for row in rows}

    def find_by_email_domain(self, email: str) -> VendorModel | None:
        """Inbound mail routing: match the sender back to a known vendor."""
        address = email.strip().lower()
        exact = self.session.scalars(
            self._scoped(select(VendorModel), VendorModel).where(
                func.lower(VendorModel.email) == address
            )
        ).first()
        if exact:
            return exact
        contact = self.session.scalars(
            self._scoped(select(VendorContactModel), VendorContactModel).where(
                func.lower(VendorContactModel.email) == address
            )
        ).first()
        if contact:
            return self.get(contact.vendor_id)
        if "@" in address:
            domain = address.split("@", 1)[1]
            return self.session.scalars(
                self._scoped(select(VendorModel), VendorModel)
                .where(VendorModel.email.ilike(f"%@{domain}"))
                .limit(1)
            ).first()
        return None

    def get_contacts(self, vendor_id: str, *, rfq_only: bool = False) -> list[VendorContactModel]:
        stmt = self._scoped(select(VendorContactModel), VendorContactModel).where(
            VendorContactModel.vendor_id == vendor_id, VendorContactModel.active.is_(True)
        )
        if rfq_only:
            stmt = stmt.where(VendorContactModel.is_primary_rfq_contact.is_(True))
        return list(
            self.session.scalars(
                stmt.order_by(desc(VendorContactModel.is_primary_rfq_contact))
            ).all()
        )

    def primary_rfq_email(self, vendor: VendorModel) -> tuple[str, str]:
        contacts = self.get_contacts(vendor.vendor_id, rfq_only=True) or self.get_contacts(
            vendor.vendor_id
        )
        if contacts:
            return contacts[0].email, contacts[0].name
        return vendor.email, vendor.name

    def list_qualified(
        self,
        *,
        countries: Sequence[str] | None = None,
        capability_tags: Sequence[str] | None = None,
        limit: int = 200,
    ) -> list[VendorModel]:
        stmt = self._scoped(select(VendorModel), VendorModel).where(
            VendorModel.status == "ACTIVE", VendorModel.qualified.is_(True)
        )
        if countries:
            stmt = stmt.where(VendorModel.country.in_(list(countries)))
        rows = list(self.session.scalars(stmt.limit(limit * 4)).all())
        if capability_tags:
            wanted = {t.lower() for t in capability_tags}
            rows = [
                row
                for row in rows
                if wanted & {str(tag).lower() for tag in (row.capability_tags or [])}
            ]
        return rows[:limit]

    def semantic_search(
        self, query_vector: Sequence[float], *, top_k: int = 10, dimensions: int = 1024
    ) -> list[tuple[str, float]]:
        searcher = VectorSearch(dimensions=dimensions, candidate_limit=2_000)
        hits = searcher.search(
            self.session.connection(),
            table="vendors",
            id_column="vendor_id",
            embedding_column="embedding",
            query_vector=query_vector,
            top_k=top_k,
            where_sql=(
                "tenant_id = :tenant_id AND embedding IS NOT NULL "
                "AND status = 'ACTIVE' AND qualified = true"
            ),
            params={"tenant_id": self.tenant_id},
        )
        return [(hit.row_id, hit.score) for hit in hits]

    def approved_for_material(
        self, material_code: str, plant_code: str = "", *, as_of: datetime | None = None
    ) -> list[str]:
        as_of = as_of or utcnow()
        stmt = self._scoped(select(SourceListModel.vendor_id), SourceListModel).where(
            SourceListModel.material_code == material_code,
            SourceListModel.blocked.is_(False),
            SourceListModel.valid_from <= as_of,
            or_(SourceListModel.valid_to.is_(None), SourceListModel.valid_to >= as_of),
        )
        if plant_code:
            stmt = stmt.where(SourceListModel.plant_code == plant_code)
        return sorted(set(self.session.scalars(stmt).all()))

    def fixed_source_for_material(
        self, material_code: str, plant_code: str = ""
    ) -> str | None:
        stmt = self._scoped(select(SourceListModel), SourceListModel).where(
            SourceListModel.material_code == material_code,
            SourceListModel.fixed_source.is_(True),
            SourceListModel.blocked.is_(False),
        )
        if plant_code:
            stmt = stmt.where(SourceListModel.plant_code == plant_code)
        row = self.session.scalars(stmt.limit(1)).first()
        return row.vendor_id if row else None


class SqlEnterpriseHistoryRepository(TenantScopedRepository):
    """The historical purchasing toolbox (pipeline stage 3)."""

    def get_last_purchases(
        self,
        material_code: str,
        limit: int = 10,
        *,
        plant_code: str = "",
        vendor_id: str = "",
        since_months: int | None = None,
    ) -> list[dict[str, Any]]:
        stmt = self._base_query(material_code, plant_code, vendor_id, since_months)
        rows = self.session.scalars(
            stmt.order_by(desc(PH.order_date)).limit(min(limit, 500))
        ).all()
        return [_purchase_row(row) for row in rows]

    def get_price_statistics(
        self,
        material_code: str,
        *,
        months: int = 36,
        plant_code: str = "",
        base_currency: str = "USD",
    ) -> dict[str, Any]:
        """Aggregate price behaviour for a material over a window.

        Prices are compared in base currency using the exchange rate captured on
        the PO itself, which is what the ERP actually paid.
        """
        cutoff = utcnow() - timedelta(days=30 * months)
        unit_base = _unit_price_base()
        stmt = (
            select(
                func.count().label("order_count"),
                func.min(unit_base).label("min_price"),
                func.max(unit_base).label("max_price"),
                func.avg(unit_base).label("avg_price"),
                func.sum(PH.quantity).label("total_quantity"),
                func.sum(PH.net_value_base).label("total_spend"),
                func.count(func.distinct(PH.vendor_id)).label("vendor_count"),
                func.min(PH.order_date).label("first_order"),
                func.max(PH.order_date).label("last_order"),
            )
            .where(
                PH.tenant_id == self.tenant_id,
                PH.material_code == material_code,
                PH.order_date >= cutoff,
                PH.deletion_indicator.is_(False),
                PH.valid_to.is_(None),
                PH.unit_price > 0,
            )
        )
        if plant_code:
            stmt = stmt.where(PH.plant_code == plant_code)
        row = self.session.execute(stmt).one()
        if not row.order_count:
            return {
                "material_code": material_code,
                "window_months": months,
                "order_count": 0,
                "base_currency": base_currency,
            }

        median, p25, p75 = self._price_percentiles(material_code, cutoff, plant_code)
        weighted = self._weighted_average_price(material_code, cutoff, plant_code)
        return {
            "material_code": material_code,
            "window_months": months,
            "order_count": int(row.order_count),
            "vendor_count": int(row.vendor_count or 0),
            "min_unit_price": _dec(row.min_price),
            "max_unit_price": _dec(row.max_price),
            "avg_unit_price": _dec(row.avg_price),
            "median_unit_price": median,
            "p25_unit_price": p25,
            "p75_unit_price": p75,
            "weighted_avg_unit_price": weighted,
            "total_quantity": _dec(row.total_quantity),
            "total_spend_base": _dec(row.total_spend),
            "first_order_date": _iso(row.first_order),
            "last_order_date": _iso(row.last_order),
            "base_currency": base_currency,
        }

    def _price_percentiles(
        self, material_code: str, cutoff: datetime, plant_code: str
    ) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
        plant_clause = "AND plant_code = :plant_code" if plant_code else ""
        params: dict[str, Any] = {
            "tenant_id": self.tenant_id,
            "material_code": material_code,
            "cutoff": cutoff,
        }
        if plant_code:
            params["plant_code"] = plant_code
        # CockroachDB's percentile_cont only accepts a FLOAT ordering column, so
        # the DECIMAL price expression is cast explicitly. Percentiles are a
        # display and negotiation-target input, where float precision is fine;
        # nothing that becomes a price is computed from these.
        price_expr = "(unit_price * exchange_rate / GREATEST(price_unit, 1))::FLOAT8"
        sql = text(
            f"""
            SELECT
              percentile_cont(0.25) WITHIN GROUP (ORDER BY {price_expr}) AS p25,
              percentile_cont(0.50) WITHIN GROUP (ORDER BY {price_expr}) AS p50,
              percentile_cont(0.75) WITHIN GROUP (ORDER BY {price_expr}) AS p75
            FROM purchase_history
            WHERE tenant_id = :tenant_id AND material_code = :material_code
              AND order_date >= :cutoff AND deletion_indicator = false
              AND valid_to IS NULL AND unit_price > 0
              {plant_clause}
            """
        )
        try:
            # A failed statement poisons the surrounding transaction in the
            # PostgreSQL protocol, so the attempt runs inside a savepoint and the
            # fallback below still has a usable session.
            with self.session.begin_nested():
                row = self.session.execute(sql, params).one()
            return _dec(row.p50), _dec(row.p25), _dec(row.p75)
        except Exception as exc:
            log.info("percentile_aggregate_unavailable", detail=str(exc)[:160])

        # Fallback: rank a bounded sample in Python.
        prices = sorted(
            float(p)
            for p in self.session.scalars(
                self._base_query(material_code, plant_code, "", None)
                .with_only_columns(_unit_price_base())
                .where(PH.order_date >= cutoff)
                .limit(5_000)
            ).all()
        )
        if not prices:
            return None, None, None

        def pick(quantile: float) -> Decimal:
            index = min(len(prices) - 1, int(quantile * len(prices)))
            return Decimal(str(prices[index]))

        return pick(0.50), pick(0.25), pick(0.75)

    def _weighted_average_price(
        self, material_code: str, cutoff: datetime, plant_code: str
    ) -> Decimal | None:
        stmt = select(
            func.sum(PH.net_value_base).label("spend"), func.sum(PH.quantity).label("qty")
        ).where(
            PH.tenant_id == self.tenant_id,
            PH.material_code == material_code,
            PH.order_date >= cutoff,
            PH.deletion_indicator.is_(False),
            PH.valid_to.is_(None),
            PH.quantity > 0,
        )
        if plant_code:
            stmt = stmt.where(PH.plant_code == plant_code)
        row = self.session.execute(stmt).one()
        if not row.qty or Decimal(str(row.qty)) == 0:
            return None
        return (Decimal(str(row.spend or 0)) / Decimal(str(row.qty))).quantize(Decimal("0.000001"))

    def get_price_trend(
        self, material_code: str, *, months: int = 24, plant_code: str = ""
    ) -> list[dict[str, Any]]:
        """Monthly weighted-average price series, for escalation detection."""
        cutoff = utcnow() - timedelta(days=30 * months)
        bucket = func.date_trunc("month", PH.order_date).label("month")
        stmt = (
            select(
                bucket,
                func.sum(PH.net_value_base).label("spend"),
                func.sum(PH.quantity).label("qty"),
                func.count().label("orders"),
            )
            .where(
                PH.tenant_id == self.tenant_id,
                PH.material_code == material_code,
                PH.order_date >= cutoff,
                PH.deletion_indicator.is_(False),
                PH.valid_to.is_(None),
                PH.quantity > 0,
            )
            .group_by(bucket)
            .order_by(bucket)
        )
        if plant_code:
            stmt = stmt.where(PH.plant_code == plant_code)
        out: list[dict[str, Any]] = []
        for row in self.session.execute(stmt).all():
            qty = Decimal(str(row.qty or 0))
            out.append(
                {
                    "month": _iso(row.month)[:7],
                    "orders": int(row.orders),
                    "quantity": _dec(row.qty),
                    "spend_base": _dec(row.spend),
                    "weighted_avg_unit_price": (
                        (Decimal(str(row.spend or 0)) / qty).quantize(Decimal("0.000001"))
                        if qty
                        else None
                    ),
                }
            )
        return out

    def get_vendors_for_material(
        self, material_code: str, *, months: int = 36, limit: int = 25
    ) -> list[dict[str, Any]]:
        """Who has supplied this material, at what price, how often."""
        cutoff = utcnow() - timedelta(days=30 * months)
        unit_base = _unit_price_base()
        stmt = (
            select(
                PH.vendor_id,
                func.max(PH.vendor_name).label("vendor_name"),
                func.count().label("order_count"),
                func.sum(PH.quantity).label("total_quantity"),
                func.sum(PH.net_value_base).label("total_spend"),
                func.min(unit_base).label("min_price"),
                func.avg(unit_base).label("avg_price"),
                func.max(PH.order_date).label("last_order_date"),
                func.avg(PH.days_late).label("avg_days_late"),
            )
            .where(
                PH.tenant_id == self.tenant_id,
                PH.material_code == material_code,
                PH.order_date >= cutoff,
                PH.deletion_indicator.is_(False),
                PH.valid_to.is_(None),
            )
            .group_by(PH.vendor_id)
            .order_by(desc(func.sum(PH.net_value_base)))
            .limit(limit)
        )
        results: list[dict[str, Any]] = []
        for row in self.session.execute(stmt).all():
            qty = Decimal(str(row.total_quantity or 0))
            results.append(
                {
                    "vendor_id": row.vendor_id,
                    "vendor_name": row.vendor_name,
                    "order_count": int(row.order_count),
                    "total_quantity": _dec(row.total_quantity),
                    "total_spend_base": _dec(row.total_spend),
                    "min_unit_price": _dec(row.min_price),
                    "avg_unit_price": _dec(row.avg_price),
                    "weighted_avg_unit_price": (
                        (Decimal(str(row.total_spend or 0)) / qty).quantize(Decimal("0.000001"))
                        if qty
                        else None
                    ),
                    "last_order_date": _iso(row.last_order_date),
                    "avg_days_late": float(row.avg_days_late or 0),
                }
            )
        return results

    def get_approved_suppliers(self, material_code: str) -> list[dict[str, Any]]:
        """Kept for the historical-context service: distinct recent suppliers."""
        return [
            {"vendor_id": v["vendor_id"], "vendor_name": v["vendor_name"]}
            for v in self.get_vendors_for_material(material_code, months=60, limit=50)
        ]

    def get_last_price_from_vendor(
        self, material_code: str, vendor_id: str
    ) -> dict[str, Any] | None:
        row = self.session.scalars(
            self._base_query(material_code, "", vendor_id, None)
            .order_by(desc(PH.order_date))
            .limit(1)
        ).first()
        return _purchase_row(row) if row else None

    def get_vendor_performance(
        self, vendor_id: str, *, months: int = 24
    ) -> dict[str, Any]:
        """Delivery and quality performance computed from GR history."""
        cutoff = utcnow() - timedelta(days=30 * months)
        gr = GoodsReceiptHistoryModel
        # quantity, rejected_quantity and days_late are all NOT NULL, so no
        # COALESCE is needed - and CockroachDB will not unify a DECIMAL column
        # with an INT literal inside one, which is how this first surfaced.
        row = self.session.execute(
            select(
                func.count().label("receipts"),
                func.sum(gr.quantity).label("qty"),
                func.sum(gr.rejected_quantity).label("rejected"),
                func.avg(gr.days_late).label("avg_days_late"),
                func.sum(case((gr.days_late <= 0, 1), else_=0)).label("on_time_count"),
            ).where(
                gr.tenant_id == self.tenant_id,
                gr.vendor_id == vendor_id,
                gr.posting_date >= cutoff,
            )
        ).one()
        receipts = int(row.receipts or 0)
        spend = self.session.scalar(
            select(func.sum(PH.net_value_base)).where(
                PH.tenant_id == self.tenant_id,
                PH.vendor_id == vendor_id,
                PH.order_date >= cutoff,
                PH.deletion_indicator.is_(False),
                PH.valid_to.is_(None),
            )
        )
        qty = Decimal(str(row.qty or 0))
        rejected = Decimal(str(row.rejected or 0))
        return {
            "vendor_id": vendor_id,
            "window_months": months,
            "receipt_count": receipts,
            "on_time_pct": round(100.0 * int(row.on_time_count or 0) / receipts, 2) if receipts else None,
            "avg_days_late": round(float(row.avg_days_late or 0), 2),
            "received_quantity": _dec(row.qty),
            "rejected_quantity": _dec(row.rejected),
            "rejection_pct": (
                float((rejected / qty * Decimal(100)).quantize(Decimal("0.01"))) if qty else None
            ),
            "defect_ppm": int(rejected / qty * Decimal(1_000_000)) if qty else None,
            "spend_base": _dec(spend),
        }

    def get_vendors_by_material_group(
        self, material_group: str, *, months: int = 36, limit: int = 40
    ) -> list[dict[str, Any]]:
        """Adjacent-category sourcing: who supplies similar things well."""
        cutoff = utcnow() - timedelta(days=30 * months)
        stmt = (
            select(
                PH.vendor_id,
                func.max(PH.vendor_name).label("vendor_name"),
                func.count().label("order_count"),
                func.count(func.distinct(PH.material_code)).label("material_count"),
                func.sum(PH.net_value_base).label("total_spend"),
                func.max(PH.order_date).label("last_order_date"),
            )
            .where(
                PH.tenant_id == self.tenant_id,
                PH.material_group == material_group,
                PH.order_date >= cutoff,
                PH.deletion_indicator.is_(False),
                PH.valid_to.is_(None),
            )
            .group_by(PH.vendor_id)
            .order_by(desc(func.sum(PH.net_value_base)))
            .limit(limit)
        )
        return [
            {
                "vendor_id": row.vendor_id,
                "vendor_name": row.vendor_name,
                "order_count": int(row.order_count),
                "material_count": int(row.material_count),
                "total_spend_base": _dec(row.total_spend),
                "last_order_date": _iso(row.last_order_date),
            }
            for row in self.session.execute(stmt).all()
        ]

    def get_quantity_price_curve(
        self, material_code: str, *, months: int = 60
    ) -> list[dict[str, Any]]:
        """Observed price-vs-quantity points; the basis of should-cost."""
        cutoff = utcnow() - timedelta(days=30 * months)
        rows = self.session.execute(
            select(PH.quantity, _unit_price_base().label("unit_price"), PH.order_date)
            .where(
                PH.tenant_id == self.tenant_id,
                PH.material_code == material_code,
                PH.order_date >= cutoff,
                PH.quantity > 0,
                PH.unit_price > 0,
                PH.deletion_indicator.is_(False),
                PH.valid_to.is_(None),
            )
            .order_by(PH.quantity)
            .limit(2_000)
        ).all()
        return [
            {
                "quantity": _dec(row.quantity),
                "unit_price_base": _dec(row.unit_price),
                "order_date": _iso(row.order_date),
            }
            for row in rows
        ]

    def get_spend_summary(self, *, months: int = 12, limit: int = 20) -> dict[str, Any]:
        cutoff = utcnow() - timedelta(days=30 * months)
        base = [
            PH.tenant_id == self.tenant_id,
            PH.order_date >= cutoff,
            PH.deletion_indicator.is_(False),
            PH.valid_to.is_(None),
        ]
        total = self.session.scalar(select(func.sum(PH.net_value_base)).where(*base))
        by_vendor = self.session.execute(
            select(PH.vendor_id, func.max(PH.vendor_name), func.sum(PH.net_value_base).label("spend"))
            .where(*base)
            .group_by(PH.vendor_id)
            .order_by(desc("spend"))
            .limit(limit)
        ).all()
        by_group = self.session.execute(
            select(PH.material_group, func.sum(PH.net_value_base).label("spend"))
            .where(*base)
            .group_by(PH.material_group)
            .order_by(desc("spend"))
            .limit(limit)
        ).all()
        return {
            "window_months": months,
            "total_spend_base": _dec(total),
            "top_vendors": [
                {"vendor_id": v, "vendor_name": n, "spend_base": _dec(s)} for v, n, s in by_vendor
            ],
            "top_material_groups": [
                {"material_group": g, "spend_base": _dec(s)} for g, s in by_group
            ],
        }

    def _base_query(
        self, material_code: str, plant_code: str, vendor_id: str, since_months: int | None
    ):
        stmt = select(PH).where(
            PH.tenant_id == self.tenant_id,
            PH.material_code == material_code,
            PH.deletion_indicator.is_(False),
            PH.valid_to.is_(None),
        )
        if plant_code:
            stmt = stmt.where(PH.plant_code == plant_code)
        if vendor_id:
            stmt = stmt.where(PH.vendor_id == vendor_id)
        if since_months:
            stmt = stmt.where(PH.order_date >= utcnow() - timedelta(days=30 * since_months))
        return stmt


class SqlInfoRecordRepository(TenantScopedRepository):
    def get_active(
        self, material_code: str, vendor_id: str, plant_code: str = ""
    ) -> InfoRecordModel | None:
        stmt = self._scoped(select(InfoRecordModel), InfoRecordModel).where(
            InfoRecordModel.material_code == material_code,
            InfoRecordModel.vendor_id == vendor_id,
            InfoRecordModel.is_active.is_(True),
        )
        if plant_code:
            stmt = stmt.where(
                or_(InfoRecordModel.plant_code == plant_code, InfoRecordModel.plant_code == "")
            )
        return self.session.scalars(stmt.order_by(desc(InfoRecordModel.valid_from))).first()

    def list_for_material(self, material_code: str) -> list[InfoRecordModel]:
        return list(
            self.session.scalars(
                self._scoped(select(InfoRecordModel), InfoRecordModel)
                .where(
                    InfoRecordModel.material_code == material_code,
                    InfoRecordModel.is_active.is_(True),
                )
                .order_by(InfoRecordModel.net_price)
            ).all()
        )

    def create(self, **fields: Any) -> InfoRecordModel:
        row = InfoRecordModel(tenant_id=self.tenant_id, **fields)
        self.session.add(row)
        self.session.flush()
        return row

    def supersede(self, existing_id: str, replacement: InfoRecordModel) -> None:
        row = self.session.get(InfoRecordModel, existing_id)
        if row is None:
            return
        row.is_active = False
        row.valid_to = utcnow()
        row.superseded_by_id = replacement.id
        self.session.flush()

    def next_number(self) -> str:
        count = int(
            self.session.scalar(
                self._scoped(select(func.count()).select_from(InfoRecordModel), InfoRecordModel)
            )
            or 0
        )
        return f"IR{count + 1:09d}"


class SqlContractRepository(TenantScopedRepository):
    def active_for_vendor(self, vendor_id: str, *, as_of: datetime | None = None) -> list[ContractModel]:
        as_of = as_of or utcnow()
        return list(
            self.session.scalars(
                self._scoped(select(ContractModel), ContractModel).where(
                    ContractModel.vendor_id == vendor_id,
                    ContractModel.is_active.is_(True),
                    ContractModel.valid_from <= as_of,
                    ContractModel.valid_to >= as_of,
                )
            ).all()
        )

    def find_for_material(
        self, material_code: str, *, as_of: datetime | None = None
    ) -> list[ContractModel]:
        as_of = as_of or utcnow()
        rows = self.session.scalars(
            self._scoped(select(ContractModel), ContractModel)
            .where(
                ContractModel.is_active.is_(True),
                ContractModel.valid_from <= as_of,
                ContractModel.valid_to >= as_of,
            )
            .limit(1_000)
        ).all()
        return [r for r in rows if material_code in {str(m) for m in (r.materials or [])}]


class SqlFxRepository(TenantScopedRepository):
    def load_table(
        self, *, base_currency: str = "USD", since: date | None = None
    ) -> FxRateTable:
        table = FxRateTable(base_currency=base_currency)
        stmt = self._scoped(select(FxRateModel), FxRateModel)
        if since:
            stmt = stmt.where(FxRateModel.as_of >= since)
        for row in self.session.scalars(stmt.order_by(FxRateModel.as_of)).all():
            table.add(
                FxRate(
                    base=row.base_currency,
                    quote=row.quote_currency,
                    rate=Decimal(str(row.rate)),
                    as_of=row.as_of if isinstance(row.as_of, date) else row.as_of.date(),
                    source=row.source,
                )
            )
        return table

    def latest_rate(self, base: str, quote: str) -> Decimal | None:
        row = self.session.scalars(
            self._scoped(select(FxRateModel), FxRateModel)
            .where(FxRateModel.base_currency == base, FxRateModel.quote_currency == quote)
            .order_by(desc(FxRateModel.as_of))
            .limit(1)
        ).first()
        return Decimal(str(row.rate)) if row else None

    def upsert(self, rate: FxRate) -> None:
        existing = self.session.scalars(
            self._scoped(select(FxRateModel), FxRateModel).where(
                FxRateModel.base_currency == rate.base,
                FxRateModel.quote_currency == rate.quote,
                FxRateModel.as_of == rate.as_of,
                FxRateModel.source == rate.source,
            )
        ).first()
        if existing:
            existing.rate = rate.rate
        else:
            self.session.add(
                FxRateModel(
                    tenant_id=self.tenant_id,
                    base_currency=rate.base,
                    quote_currency=rate.quote,
                    rate=rate.rate,
                    as_of=rate.as_of,
                    source=rate.source,
                )
            )
        self.session.flush()


class SqlFreightRepository(TenantScopedRepository):
    def get_lane(
        self, origin_country: str, destination_plant: str, mode: str = ""
    ) -> FreightRateModel | None:
        stmt = self._scoped(select(FreightRateModel), FreightRateModel).where(
            FreightRateModel.origin_country == origin_country,
            FreightRateModel.destination_plant == destination_plant,
        )
        if mode:
            stmt = stmt.where(FreightRateModel.mode == mode)
        row = self.session.scalars(stmt.order_by(FreightRateModel.cost_per_kg)).first()
        if row:
            return row
        # Fall back to any lane out of the same origin, then give up cleanly.
        return self.session.scalars(
            self._scoped(select(FreightRateModel), FreightRateModel)
            .where(FreightRateModel.origin_country == origin_country)
            .limit(1)
        ).first()


# ------------------------------------------------------------------ helpers

def _unit_price_base():
    """Unit price expressed in base currency, respecting SAP price units."""
    return PH.unit_price * PH.exchange_rate / func.greatest(PH.price_unit, 1)


def _purchase_row(row: PurchaseHistoryModel) -> dict[str, Any]:
    price_unit = max(int(row.price_unit or 1), 1)
    return {
        "evidence_id": row.id,
        "snapshot_id": row.snapshot_id,
        "po_number": row.po_number,
        "po_line": row.po_line,
        "vendor_id": row.vendor_id,
        "vendor_name": row.vendor_name,
        "material_code": row.material_code,
        "material_description": row.material_description,
        "plant_code": row.plant_code,
        "quantity": _dec(row.quantity),
        "uom": row.uom,
        "unit_price": _dec(row.unit_price),
        "price_unit": price_unit,
        "currency": row.currency,
        "exchange_rate": _dec(row.exchange_rate),
        "unit_price_base": _dec(
            Decimal(str(row.unit_price)) * Decimal(str(row.exchange_rate or 1)) / price_unit
        ),
        "net_value": _dec(row.net_value),
        "net_value_base": _dec(row.net_value_base),
        "order_date": _iso(row.order_date),
        "delivery_date": _iso(row.delivery_date),
        "actual_delivery_date": _iso(row.actual_delivery_date),
        "incoterm": row.incoterm,
        "payment_terms": row.payment_terms,
        "days_late": int(row.days_late or 0),
        "on_time": row.on_time,
        "contract_number": row.contract_number,
        "info_record_number": row.info_record_number,
    }


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _iso(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _tokenize(text_value: str) -> list[str]:
    return [t for t in "".join(c if c.isalnum() else " " for c in text_value.lower()).split() if t]


def _strip_punctuation(value: str) -> str:
    return "".join(c for c in value.upper() if c.isalnum())
