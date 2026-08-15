"""CockroachDB transaction retry.

CockroachDB is serializable by default, so contended transactions abort with
SQLSTATE 40001 and *must* be retried by the client. This is not an error path,
it is the concurrency-control protocol. Every write that can contend goes
through `run_in_transaction`.

The rule the whole codebase follows: no LLM call, no S3 call and no SMTP call
inside a retryable block, because a retry would duplicate the side effect.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any, TypeVar

from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session

from procureguard.observability import METRICS, logger

log = logger(__name__)

T = TypeVar("T")

# 40001 serialization_failure, 40003 statement_completion_unknown,
# 08006/08003 connection failures worth one more attempt.
RETRYABLE_SQLSTATES = frozenset({"40001", "40003", "08006", "08003", "57P01"})


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (DBAPIError, OperationalError)):
        sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
        if sqlstate is None:
            sqlstate = getattr(getattr(exc, "orig", None), "pgcode", None)
        if sqlstate in RETRYABLE_SQLSTATES:
            return True
        message = str(exc).lower()
        return (
            "restart transaction" in message
            or "retry_serializable" in message
            or "retry_write_too_old" in message
        )
    return False


def run_in_transaction(
    session: Session,
    operation: Callable[[Session], T],
    *,
    max_retries: int = 5,
    base_delay_ms: int = 50,
    max_delay_ms: int = 2_000,
    label: str = "txn",
) -> T:
    """Execute `operation` inside a transaction, retrying serialization failures.

    `operation` must be idempotent with respect to database state, because it
    may run several times.
    """
    attempt = 0
    while True:
        try:
            result = operation(session)
            session.commit()
            if attempt:
                METRICS.increment("db.transaction.retried", label=label)
                log.info("txn_succeeded_after_retry", label=label, attempts=attempt + 1)
            return result
        except Exception as exc:
            session.rollback()
            if not is_retryable(exc) or attempt >= max_retries:
                if is_retryable(exc):
                    METRICS.increment("db.transaction.retry_exhausted", label=label)
                    log.error("txn_retry_exhausted", label=label, attempts=attempt + 1)
                raise
            delay_ms = min(max_delay_ms, base_delay_ms * (2**attempt))
            # Full jitter: avoids retry convoys when many workers contend.
            time.sleep(random.uniform(0, delay_ms) / 1000.0)
            attempt += 1
            log.info("txn_retry", label=label, attempt=attempt, sqlstate=_sqlstate(exc))


def _sqlstate(exc: BaseException) -> str:
    orig = getattr(exc, "orig", None)
    return str(getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None) or "unknown")


class RetryingSession:
    """Context-manager sugar for the common `with retrying(session) as s:` shape."""

    def __init__(self, session: Session, **kwargs: Any) -> None:
        self.session = session
        self.kwargs = kwargs

    def __call__(self, operation: Callable[[Session], T]) -> T:
        return run_in_transaction(self.session, operation, **self.kwargs)
