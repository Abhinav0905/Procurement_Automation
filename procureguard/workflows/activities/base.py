"""Activity infrastructure.

Every activity follows the same shape: open a session, build a ServiceContext,
call one application service, commit, return a JSON-safe dict.

Two rules the whole activity layer obeys, both of which exist because Temporal
delivers activities *at least once*:

* Side effects that leave the system (email, ERP writes) are guarded by an
  idempotency key, so a retry after a lost ack cannot duplicate them.
* No transaction stays open across a remote call. The session is committed
  before returning, and long remote work happens outside `with_context`.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any, TypeVar

from procureguard.config import get_settings
from procureguard.infrastructure.db.session import get_session_factory
from procureguard.infrastructure.factory import ServiceContext
from procureguard.observability import METRICS, log_context, logger

log = logger(__name__)

T = TypeVar("T")


@contextmanager
def with_context(
    *, tenant_id: str = "", actor_id: str = "SYSTEM", correlation_id: str = ""
) -> Iterator[ServiceContext]:
    """Session-scoped ServiceContext that commits on success."""
    settings = get_settings()
    session = get_session_factory()()
    try:
        ctx = ServiceContext.build(
            session,
            settings=settings,
            tenant_id=tenant_id or settings.default_tenant_id,
            actor_id=actor_id,
            correlation_id=correlation_id,
        )
        yield ctx
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def activity_handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Log, time and bind context around an activity body."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapper(args: dict[str, Any]) -> Any:
            args = args or {}
            started = time.perf_counter()
            with log_context(
                activity=name,
                case_id=args.get("case_id"),
                tenant_id=args.get("tenant_id"),
            ):
                log.info("activity_started", **_loggable(args))
                try:
                    result = await func(args)
                except Exception as exc:
                    METRICS.increment("activity.failed", activity=name)
                    log.error("activity_failed", detail=str(exc)[:500], error=type(exc).__name__)
                    raise
                elapsed = (time.perf_counter() - started) * 1000
                METRICS.observe("activity.latency", elapsed, activity=name)
                METRICS.increment("activity.completed", activity=name)
                log.info("activity_completed", duration_ms=round(elapsed, 1))
                return jsonable(result)

        return wrapper

    return decorator


def jsonable(value: Any) -> Any:
    """Convert service results into something Temporal can serialise."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if hasattr(value, "to_dict"):
        return jsonable(value.to_dict())
    return str(value)


def _loggable(args: dict[str, Any]) -> dict[str, Any]:
    """Keep bulky payloads out of the log line."""
    out: dict[str, Any] = {}
    for key, item in args.items():
        if key in ("content", "body", "text", "raw"):
            out[f"{key}_bytes"] = len(item) if item else 0
            continue
        if isinstance(item, (str, int, float, bool)) and len(str(item)) <= 120:
            out[key] = item
    return out
