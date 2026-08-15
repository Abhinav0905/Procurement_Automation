"""Operational routes: health, mailroom, analytics, master-data lookup, admin."""

from __future__ import annotations

import base64
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from procureguard.api.dependencies import Context, CurrentPrincipal, require
from procureguard.api.schemas import (
    HealthResponse,
    InboundEmailWebhook,
    ReleaseCommunicationRequest,
)
from procureguard.api.temporal_client import try_get_temporal_client
from procureguard.application.history_service import HistoricalProcurementService
from procureguard.application.mailroom import MailroomService
from procureguard.application.quotation_ingestion import QuotationIngestionService
from procureguard.config import get_settings
from procureguard.domain.enums import CommunicationType, Permission
from procureguard.domain.errors import NotFoundError, ValidationError
from procureguard.infrastructure.db.session import healthcheck
from procureguard.observability import METRICS, logger

log = logger(__name__)

health_router = APIRouter(tags=["health"])
mail_router = APIRouter(prefix="/mail", tags=["mailroom"])
data_router = APIRouter(prefix="/data", tags=["master-data"])
admin_router = APIRouter(prefix="/admin", tags=["admin"])


# ═══════════════════════════════════════════════════════════════════ health

@health_router.get("/health")
def health() -> dict[str, str]:
    """Liveness. Deliberately dependency-free so it never flaps."""
    return {"status": "ok"}


@health_router.get("/health/ready", response_model=HealthResponse)
async def readiness() -> HealthResponse:
    """Readiness. Reports each dependency without failing the whole check."""
    settings = get_settings()
    database = healthcheck()
    client = await try_get_temporal_client()
    temporal = (
        {"status": "ok", "address": settings.temporal_address, "namespace": settings.temporal_namespace}
        if client is not None
        else {"status": "unavailable", "address": settings.temporal_address}
    )
    return HealthResponse(
        status="ok" if database.get("status") == "ok" else "degraded",
        version=settings.app_version,
        environment=settings.app_env,
        database=database,
        temporal=temporal,
        backends={
            "object_store": settings.object_store_backend,
            "llm": settings.llm_backend,
            "embeddings": settings.embedding_backend,
            "email": settings.email_backend,
            "encryption": settings.encryption_backend,
            "auth": settings.auth_mode,
            "vector": database.get("vector_backend", "unknown"),
        },
    )


@health_router.get("/metrics")
def metrics(principal: CurrentPrincipal) -> dict[str, Any]:
    return METRICS.snapshot()


@health_router.get("/whoami")
def whoami(principal: CurrentPrincipal) -> dict[str, Any]:
    return principal.to_dict()


# ═════════════════════════════════════════════════════════════════ mailroom

@mail_router.post("/inbound")
def inbound_webhook(
    body: InboundEmailWebhook,
    ctx: Context,
    principal: CurrentPrincipal,
) -> dict[str, Any]:
    """Receive a supplier reply.

    Accepts a raw MIME body (testing and SMTP relays), an S3 key (SES inbound),
    or an SNS/EventBridge notification wrapping one.
    """
    from procureguard.infrastructure.email.receiver import parse_mime

    mailroom = MailroomService(ctx)
    ingestion = QuotationIngestionService(ctx)
    messages = []

    if body.raw_message:
        raw = body.raw_message.encode() if isinstance(body.raw_message, str) else body.raw_message
        try:
            raw = base64.b64decode(raw, validate=True)
        except Exception:
            pass  # already plain MIME text
        messages.append(parse_mime(raw))
    elif body.s3_key or body.notification:
        from procureguard.infrastructure.email.receiver import SesS3MailReceiver

        receiver = SesS3MailReceiver(ctx.settings)
        if body.s3_key:
            messages.append(receiver.fetch_from_key(body.s3_key))
        else:
            messages.extend(receiver.fetch_from_notification(body.notification or {}))
    else:
        raise ValidationError("Provide raw_message, s3_key or notification")

    results = []
    for message in messages:
        outcome = mailroom.receive(message)
        entry = outcome.to_dict()
        if (
            outcome.case_id
            and not outcome.quarantined
            and outcome.classification
            in (
                CommunicationType.QUOTATION_RECEIPT.value,
                CommunicationType.NEGOTIATION_RESPONSE.value,
            )
        ):
            try:
                entry["quotation"] = ingestion.ingest_from_communication(
                    outcome.communication_id
                ).to_dict()
            except Exception as exc:
                entry["quotation_error"] = str(exc)[:300]
        results.append(entry)
    return {"received": len(results), "results": results}


@mail_router.post("/poll")
def poll_inbox(
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.EMAIL_SEND))],
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """Pull the shared mailbox now rather than waiting for the workflow tick."""
    mailroom = MailroomService(ctx)
    ingestion = QuotationIngestionService(ctx)
    results = []
    for outcome in mailroom.poll(limit=limit):
        entry = outcome.to_dict()
        if outcome.case_id and not outcome.quarantined and outcome.classification in (
            CommunicationType.QUOTATION_RECEIPT.value,
            CommunicationType.NEGOTIATION_RESPONSE.value,
        ):
            try:
                entry["quotation"] = ingestion.ingest_from_communication(
                    outcome.communication_id
                ).to_dict()
            except Exception as exc:
                entry["quotation_error"] = str(exc)[:300]
        results.append(entry)
    return {"processed": len(results), "results": results}


@mail_router.get("/pending")
def pending_release(
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.EMAIL_SEND))],
) -> dict[str, Any]:
    """Outbound messages the policy gate is holding for a human."""
    return {
        "messages": [
            {
                "communication_id": c.id,
                "case_id": c.case_id,
                "vendor_id": c.vendor_id,
                "type": c.communication_type,
                "to": c.to_addresses,
                "subject": c.subject,
                "body_preview": (c.body_text or "")[:1500],
                "status": c.status,
                "held_reason": c.error_detail,
                "attachments": [a.get("filename") for a in (c.attachment_refs or [])],
                "created_at": c.created_at.isoformat(),
            }
            for c in ctx.repos.communications.list_pending_release()
        ]
    }


@mail_router.post("/{communication_id}/release")
def release_communication(
    communication_id: str,
    body: ReleaseCommunicationRequest,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.EMAIL_SEND))],
) -> dict[str, Any]:
    """Transmit a held message under a named human's authority."""
    outcome = MailroomService(ctx).release_held(communication_id, actor_id=principal.actor_id)
    return {**outcome.to_dict(), "released_by": principal.actor_id, "reason": body.reason}


@mail_router.get("/case/{case_id}")
def case_correspondence(
    case_id: str,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.CASE_READ))],
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "messages": [
            {
                "communication_id": c.id,
                "direction": c.direction,
                "type": c.communication_type,
                "status": c.status,
                "vendor_id": c.vendor_id,
                "from": c.from_address,
                "to": c.to_addresses,
                "subject": c.subject,
                "body_preview": (c.body_text or "")[:2000],
                "sent_at": c.sent_at.isoformat() if c.sent_at else None,
                "received_at": c.received_at.isoformat() if c.received_at else None,
            }
            for c in ctx.repos.communications.list_for_case(case_id)
        ],
    }


# ══════════════════════════════════════════════════════════════ master data

@data_router.get("/materials/search")
def search_materials(
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.CASE_READ))],
    q: str = Query(min_length=2),
    limit: int = Query(default=20, ge=1, le=100),
    semantic: bool = True,
) -> dict[str, Any]:
    """Lexical and vector search over the material master."""
    results: dict[str, dict[str, Any]] = {}
    for material in ctx.repos.materials.search_text(q, limit=limit):
        results[material.material_code] = _material_payload(material, "keyword", None)

    if semantic:
        try:
            vector = ctx.embedder.embed(q)
            for code, score in ctx.repos.materials.semantic_search(
                vector, top_k=limit, dimensions=ctx.embedder.dimensions
            ):
                if code in results:
                    results[code]["match"] = "hybrid"
                    results[code]["similarity"] = round(score, 4)
                    continue
                material = ctx.repos.materials.get(code)
                if material:
                    results[code] = _material_payload(material, "semantic", round(score, 4))
        except Exception as exc:
            log.info("material_semantic_search_unavailable", detail=str(exc)[:200])

    ranked = sorted(
        results.values(), key=lambda r: (r.get("similarity") or 0), reverse=True
    )
    return {"query": q, "count": len(ranked), "materials": ranked[:limit]}


@data_router.get("/materials/{material_code}/benchmark")
def material_benchmark(
    material_code: str,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.CASE_READ))],
    quantity: Decimal = Query(default=Decimal(1), gt=0),
    uom: str = "",
    plant_code: str = "",
    months: int = Query(default=36, ge=1, le=120),
) -> dict[str, Any]:
    """Stage 3 on demand: the full historical price picture for a material."""
    benchmark = HistoricalProcurementService(ctx).build_benchmark(
        material_code,
        requested_quantity=quantity,
        requested_uom=uom,
        plant_code=plant_code,
        window_months=months,
    )
    return benchmark.to_dict()


@data_router.get("/materials/{material_code}/purchases")
def material_purchases(
    material_code: str,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.CASE_READ))],
    limit: int = Query(default=25, ge=1, le=200),
    plant_code: str = "",
) -> dict[str, Any]:
    return {
        "material_code": material_code,
        "purchases": ctx.repos.history.get_last_purchases(
            material_code, limit, plant_code=plant_code
        ),
    }


@data_router.get("/vendors/{vendor_id}")
def vendor_detail(
    vendor_id: str,
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.CASE_READ))],
    months: int = Query(default=24, ge=1, le=120),
) -> dict[str, Any]:
    vendor = ctx.repos.vendors.get(vendor_id)
    if vendor is None:
        raise NotFoundError(f"Vendor {vendor_id} not found", vendor_id=vendor_id)
    return {
        "vendor_id": vendor.vendor_id,
        "name": vendor.name,
        "status": vendor.status,
        "country": vendor.country,
        "city": vendor.city,
        "email": vendor.email,
        "currency": vendor.currency,
        "default_incoterm": vendor.default_incoterm,
        "payment_terms": vendor.payment_terms,
        "qualified": vendor.qualified,
        "certifications": vendor.certifications,
        "capability_tags": vendor.capability_tags,
        "on_time_delivery_pct": str(vendor.on_time_delivery_pct),
        "quality_ppm": vendor.quality_ppm,
        "quote_response_rate_pct": str(vendor.quote_response_rate_pct),
        "financial_risk": vendor.financial_risk,
        "geopolitical_risk": vendor.geopolitical_risk,
        "performance": ctx.repos.history.get_vendor_performance(vendor_id, months=months),
        "contacts": [
            {"name": c.name, "email": c.email, "role": c.role, "primary": c.is_primary_rfq_contact}
            for c in ctx.repos.vendors.get_contacts(vendor_id)
        ],
    }


@data_router.get("/spend")
def spend_summary(
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.CASE_READ))],
    months: int = Query(default=12, ge=1, le=120),
) -> dict[str, Any]:
    return ctx.repos.history.get_spend_summary(months=months)


# ═════════════════════════════════════════════════════════════════════ admin

@admin_router.get("/stats")
def stats(
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.ADMIN_MANAGE))],
) -> dict[str, Any]:
    """Row counts, for verifying a seed actually landed."""
    from sqlalchemy import func, select

    from procureguard.infrastructure.db.models import (
        DocumentChunkModel,
        GoodsReceiptHistoryModel,
        InfoRecordModel,
        MaterialModel,
        PurchaseHistoryModel,
        SourcingCaseModel,
        VendorModel,
    )

    counts = {}
    for label, model in (
        ("materials", MaterialModel),
        ("vendors", VendorModel),
        ("purchase_history", PurchaseHistoryModel),
        ("goods_receipts", GoodsReceiptHistoryModel),
        ("info_records", InfoRecordModel),
        ("sourcing_cases", SourcingCaseModel),
        ("document_chunks", DocumentChunkModel),
    ):
        counts[label] = int(ctx.session.scalar(select(func.count()).select_from(model)) or 0)
    return {"tenant_id": ctx.tenant_id, "row_counts": counts}


@admin_router.post("/purge-idempotency")
def purge_idempotency(
    ctx: Context,
    principal: Annotated[Any, Depends(require(Permission.ADMIN_MANAGE))],
) -> dict[str, Any]:
    return {"purged": ctx.repos.idempotency.purge_expired()}


def _material_payload(material: Any, match: str, similarity: float | None) -> dict[str, Any]:
    return {
        "material_code": material.material_code,
        "description": material.description,
        "material_group": material.material_group,
        "material_group_text": material.material_group_text,
        "base_uom": material.base_uom,
        "status": material.status,
        "manufacturer": material.manufacturer,
        "manufacturer_part_number": material.manufacturer_part_number,
        "match": match,
        "similarity": similarity,
    }
