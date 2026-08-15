"""Engine and session management for CockroachDB.

The engine is created lazily so that tests and CLI tools can override settings
before the first connection, and so importing a model never opens a socket.
Native-vector capability is probed once, at engine creation, before any DDL is
emitted - that ordering is what lets the embedding column pick its type.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from procureguard.config import Settings, get_settings
from procureguard.observability import logger

from .vector import probe_vector_support

log = logger(__name__)

# Re-exported so `from .session import Base` keeps working across the codebase.
from .models import Base  # noqa: E402  (import after settings to avoid a cycle)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
# Reentrant: get_session_factory() needs the engine while initialising, and a
# plain Lock deadlocks the first caller whose entry point is a session rather
# than the engine - which is every API request and every Temporal activity.
_lock = threading.RLock()


def build_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    connect_args: dict[str, Any] = {
        "application_name": f"{settings.app_name}-{settings.app_env}",
        # Belt and braces against a runaway analytical query pinning a range.
        "options": f"-c statement_timeout={settings.db_statement_timeout_ms}",
    }
    engine = create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_recycle=settings.db_pool_recycle_seconds,
        pool_pre_ping=True,
        echo=settings.db_echo,
        future=True,
        connect_args=connect_args,
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn: Any, _record: Any) -> None:  # pragma: no cover - driver hook
        # CockroachDB defaults to SERIALIZABLE; being explicit documents intent
        # and protects against a cluster configured otherwise.
        try:
            with dbapi_conn.cursor() as cur:
                cur.execute("SET default_transaction_isolation = 'serializable'")
        except Exception:
            pass

    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                settings = get_settings()
                engine = build_engine(settings)
                _resolve_vector_backend(engine, settings)
                _engine = engine
    return _engine


def _resolve_vector_backend(engine: Engine, settings: Settings) -> None:
    if settings.vector_backend == "native":
        from .vector import set_vector_mode

        set_vector_mode(True)
        return
    if settings.vector_backend == "json":
        from .vector import set_vector_mode

        set_vector_mode(False)
        return
    probe_vector_support(engine)


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        # Resolve the engine before taking the lock so initialisation order is
        # explicit rather than relying on lock reentrancy.
        engine = get_engine()
        with _lock:
            if _session_factory is None:
                _session_factory = sessionmaker(
                    bind=engine, class_=Session, expire_on_commit=False, future=True
                )
    return _session_factory


class _SessionFactoryProxy:
    """Keeps `SessionFactory()` working while staying lazy."""

    def __call__(self, **kwargs: Any) -> Session:
        return get_session_factory()(**kwargs)

    def __getattr__(self, item: str) -> Any:
        return getattr(get_session_factory(), item)


SessionFactory = _SessionFactoryProxy()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope. Commits on success, rolls back on any exception."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def read_session() -> Iterator[Session]:
    """Read-only scope; never commits, so an accidental write is discarded."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def reset_engine() -> None:
    """Test helper: drop the cached engine and session factory."""
    global _engine, _session_factory
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _session_factory = None


def create_all(engine: Engine | None = None) -> None:
    """Create the schema plus the indexes SQLAlchemy cannot express."""
    engine = engine or get_engine()
    Base.metadata.create_all(bind=engine)
    _create_supplemental_indexes(engine)


def _create_supplemental_indexes(engine: Engine) -> None:
    from .models import VECTOR_INDEXED_TABLES
    from .vector import create_vector_index

    with engine.connect() as conn:
        for table, column, index_name in VECTOR_INDEXED_TABLES:
            create_vector_index(conn, table=table, column=column, index_name=index_name)
        # Trigram-style prefix search for material and vendor lookup. Inverted
        # indexes are a CockroachDB feature; failure is non-fatal.
        for statement in (
            "CREATE INVERTED INDEX IF NOT EXISTS idx_material_search_trgm "
            "ON materials (search_text gin_trgm_ops)",
            "CREATE INVERTED INDEX IF NOT EXISTS idx_vendor_search_trgm "
            "ON vendors (search_text gin_trgm_ops)",
            "CREATE INVERTED INDEX IF NOT EXISTS idx_material_attributes "
            "ON materials (attributes)",
        ):
            try:
                conn.execute(text(statement))
            except Exception as exc:
                log.info("supplemental_index_skipped", detail=str(exc)[:160])
        conn.commit()


def healthcheck() -> dict[str, Any]:
    from .vector import native_vector_enabled

    try:
        with get_engine().connect() as conn:
            version = conn.execute(text("SELECT version()")).scalar_one()
            return {
                "status": "ok",
                "server": str(version)[:120],
                "vector_backend": "native" if native_vector_enabled() else "json",
            }
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:300]}
