"""API route aggregation."""

from __future__ import annotations

from fastapi import APIRouter

from .approvals import po_router
from .approvals import router as approvals_router
from .cases import router as cases_router
from .operations import admin_router, data_router, health_router, mail_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health_router)
api_router.include_router(cases_router)
api_router.include_router(approvals_router)
api_router.include_router(po_router)
api_router.include_router(mail_router)
api_router.include_router(data_router)
api_router.include_router(admin_router)

# Kept for backwards compatibility with the original module layout.
router = api_router

__all__ = ["api_router", "router"]
