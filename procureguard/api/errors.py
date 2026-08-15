"""Domain error to HTTP mapping.

Services raise domain errors and know nothing about HTTP. This is the single
place that translates, so a new error type gets a sensible status code by
inheriting from the right base rather than by touching every route.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from procureguard.domain.errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    DomainInvariantError,
    ExternalServiceError,
    NotFoundError,
    PolicyViolationError,
    ProcureGuardError,
    SealedBidError,
    SecurityQuarantineError,
    UnsafeTransitionError,
    ValidationError,
)
from procureguard.observability import current_context, logger

log = logger(__name__)

STATUS_BY_ERROR: tuple[tuple[type[ProcureGuardError], int], ...] = (
    (AuthenticationError, 401),
    (AuthorizationError, 403),
    (NotFoundError, 404),
    (ConflictError, 409),
    (SealedBidError, 423),  # Locked - the bid is sealed, not forbidden forever
    (PolicyViolationError, 422),
    (SecurityQuarantineError, 422),
    (UnsafeTransitionError, 409),
    (DomainInvariantError, 422),
    (ValidationError, 400),
    (ExternalServiceError, 502),
)


def status_for(exc: ProcureGuardError) -> int:
    for error_type, status in STATUS_BY_ERROR:
        if isinstance(exc, error_type):
            return status
    return 500


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProcureGuardError)
    async def _domain_error(request: Request, exc: ProcureGuardError) -> JSONResponse:
        status = status_for(exc)
        payload: dict[str, Any] = {
            "error": exc.code,
            "message": exc.message,
            "context": exc.context,
            "correlation_id": current_context().get("correlation_id", ""),
        }
        # 4xx is the caller's problem and expected; 5xx is ours and is noisy.
        if status >= 500:
            log.error("request_failed", code=exc.code, path=request.url.path, detail=exc.message)
        else:
            log.info("request_rejected", code=exc.code, path=request.url.path, status=status)
        return JSONResponse(status_code=status, content=payload)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_exception", path=request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "error": "INTERNAL_ERROR",
                "message": "An unexpected error occurred",
                "correlation_id": current_context().get("correlation_id", ""),
            },
        )
