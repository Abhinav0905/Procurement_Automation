"""Structured logging, tracing and metrics.

Log lines are JSON in every environment except a developer console, and carry
case_id/tenant_id/actor context automatically via contextvars so that a single
sourcing case can be reconstructed from logs alone.
"""

from __future__ import annotations

import contextvars
import logging
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from types import MappingProxyType
from typing import Any

import structlog

# An immutable default: a mutable one would be shared by every context, so a
# value bound in one request could leak into another.
_EMPTY_CONTEXT: Mapping[str, Any] = MappingProxyType({})

_request_context: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar(
    "procureguard_context", default=_EMPTY_CONTEXT
)

_configured = False


def _context_processor(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key, value in _request_context.get().items():
        event_dict.setdefault(key, value)
    return event_dict


def _redact_processor(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Keep credentials and supplier bank details out of the log stream."""
    for key in list(event_dict):
        lowered = key.lower()
        if any(
            token in lowered
            for token in ("password", "secret", "token", "api_key", "authorization", "iban", "account_number")
        ):
            event_dict[key] = "***redacted***"
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    global _configured
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
    )
    for noisy in ("botocore", "boto3", "urllib3", "s3transfer", "temporalio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    renderer: Any = (
        structlog.dev.ConsoleRenderer(colors=True)
        if fmt == "console"
        else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _context_processor,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _redact_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )
    _configured = True


def logger(name: str) -> Any:
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)


@contextmanager
def log_context(**values: Any) -> Iterator[None]:
    """Bind values onto every log line emitted inside the block."""
    previous = _request_context.get()
    merged = {**previous, **{k: v for k, v in values.items() if v is not None}}
    token = _request_context.set(MappingProxyType(merged))
    try:
        yield
    finally:
        _request_context.reset(token)


def current_context() -> dict[str, Any]:
    return dict(_request_context.get())


def new_correlation_id() -> str:
    return uuid.uuid4().hex


# ------------------------------------------------------------------- metrics

class MetricsRegistry:
    """Minimal in-process counters/timers.

    Deliberately dependency-free: the OTLP exporter below forwards these when
    enabled, and tests can assert on them without a metrics backend.
    """

    def __init__(self) -> None:
        self._counters: dict[str, float] = {}
        self._timings: dict[str, list[float]] = {}

    def increment(self, name: str, value: float = 1.0, **labels: Any) -> None:
        self._counters[_key(name, labels)] = self._counters.get(_key(name, labels), 0.0) + value

    def observe(self, name: str, millis: float, **labels: Any) -> None:
        self._timings.setdefault(_key(name, labels), []).append(millis)

    def snapshot(self) -> dict[str, Any]:
        timings = {}
        for key, samples in self._timings.items():
            ordered = sorted(samples)
            timings[key] = {
                "count": len(ordered),
                "p50_ms": round(_percentile(ordered, 0.50), 2),
                "p95_ms": round(_percentile(ordered, 0.95), 2),
                "max_ms": round(ordered[-1], 2) if ordered else 0.0,
            }
        return {"counters": dict(self._counters), "timings": timings}

    def reset(self) -> None:
        self._counters.clear()
        self._timings.clear()


def _key(name: str, labels: dict[str, Any]) -> str:
    if not labels:
        return name
    rendered = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return f"{name}{{{rendered}}}"


def _percentile(ordered: list[float], q: float) -> float:
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


METRICS = MetricsRegistry()


@contextmanager
def timed(name: str, **labels: Any) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        METRICS.observe(name, (time.perf_counter() - started) * 1000.0, **labels)


# ------------------------------------------------------------------- tracing

def configure_tracing(settings) -> None:  # noqa: ANN001 - avoids config import cycle
    """Install OTLP tracing when enabled and the SDK is installed.

    Absence of opentelemetry is not an error; the system degrades to logs.
    """
    if not settings.otel_enabled:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger(__name__).warning(
            "otel_enabled_but_sdk_missing", hint="pip install 'procureguard[otel]'"
        )
        return

    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": settings.app_version,
            "deployment.environment": settings.app_env,
        }
    )
    provider = TracerProvider(resource=resource)
    endpoint = settings.otel_exporter_otlp_endpoint or None
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    logger(__name__).info("tracing_configured", endpoint=endpoint or "default")


def tracer(name: str = "procureguard") -> Any:
    try:
        from opentelemetry import trace

        return trace.get_tracer(name)
    except ImportError:  # pragma: no cover - optional dependency
        return _NoopTracer()


class _NoopSpan:
    def set_attribute(self, *_: Any, **__: Any) -> None: ...
    def record_exception(self, *_: Any, **__: Any) -> None: ...
    def __enter__(self) -> _NoopSpan:
        return self
    def __exit__(self, *_: Any) -> bool:
        return False


class _NoopTracer:
    def start_as_current_span(self, *_: Any, **__: Any) -> _NoopSpan:
        return _NoopSpan()
