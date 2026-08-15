"""FastAPI application factory."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from procureguard.api.errors import install_error_handlers
from procureguard.api.routes import api_router
from procureguard.config import Settings, get_settings
from procureguard.observability import (
    METRICS,
    configure_logging,
    configure_tracing,
    log_context,
    logger,
    new_correlation_id,
)

log = logger(__name__)

UI_DIR = Path(__file__).parent / "ui"

DESCRIPTION = """
Human-in-the-loop procurement agent.

The agent parses requisitions, validates them against SAP master data, researches
historical prices, shortlists suppliers, issues RFQs, chases replies, evaluates
bids technically and commercially, negotiates, and drafts a purchase order.

**It never approves anything.** Four gates require an authenticated human:
releasing an RFQ, approving the technical evaluation (which unseals the sealed
commercial bids), authorising a negotiation round, and approving the award.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    configure_tracing(settings)
    log.info(
        "api_starting",
        environment=settings.app_env,
        version=settings.app_version,
        auth_mode=settings.auth_mode,
        llm_backend=settings.llm_backend,
        email_send_enabled=settings.allow_automated_email_send,
        po_creation_enabled=settings.allow_automated_po_creation,
    )
    if settings.auth_mode == "dev":
        log.warning(
            "dev_auth_enabled",
            detail="X-Actor-Id / X-Actor-Roles headers are trusted; never use in production",
        )
    yield
    log.info("api_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title="ProcureGuard",
        version=settings.app_version,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    if not settings.is_production:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def correlation_and_timing(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        correlation_id = request.headers.get("x-correlation-id") or new_correlation_id()
        request.state.correlation_id = correlation_id
        started = time.perf_counter()
        with log_context(
            correlation_id=correlation_id,
            method=request.method,
            path=request.url.path,
        ):
            response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Correlation-Id"] = correlation_id
        response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.1f}"
        METRICS.observe("http.latency", elapsed_ms, path=request.url.path)
        METRICS.increment("http.requests", status=str(response.status_code))
        return response

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    install_error_handlers(app)
    app.include_router(api_router)

    if UI_DIR.exists():
        app.mount("/ui", StaticFiles(directory=str(UI_DIR), html=True), name="ui")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(UI_DIR / "index.html")

    return app


app = create_app()
