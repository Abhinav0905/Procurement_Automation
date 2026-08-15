"""Alembic environment.

Runs against whatever DATABASE_URL the application is configured with, so
migrations and the app can never drift onto different clusters. The vector
capability probe happens before any DDL is emitted, which is what lets the
embedding columns resolve to VECTOR(n) or JSONB.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import pool

from procureguard.config import get_settings
from procureguard.infrastructure.db import models  # noqa: F401 - registers tables
from procureguard.infrastructure.db.session import build_engine
from procureguard.infrastructure.db.vector import probe_vector_support

config = context.config
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)
target_metadata = models.Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = build_engine(settings)
    if settings.vector_backend == "auto":
        probe_vector_support(engine)
    else:
        from procureguard.infrastructure.db.vector import set_vector_mode

        set_vector_mode(settings.vector_backend == "native")

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # CockroachDB executes schema changes as implicit transactions;
            # keeping each migration in its own transaction is the safe default.
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
