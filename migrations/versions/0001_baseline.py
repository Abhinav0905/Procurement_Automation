"""Baseline schema.

Revision ID: 0001
Revises: none

This is the greenfield baseline: it materialises the full declarative metadata
in one step, then adds the indexes SQLAlchemy cannot express (CockroachDB vector
and inverted indexes). Every subsequent revision must be hand-written explicit
DDL - baselining from metadata is a one-time privilege, not a pattern.
"""

from __future__ import annotations

from alembic import op

from procureguard.infrastructure.db import models
from procureguard.infrastructure.db.models import VECTOR_INDEXED_TABLES
from procureguard.infrastructure.db.vector import create_vector_index

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_SUPPLEMENTAL_INDEXES = (
    "CREATE INVERTED INDEX IF NOT EXISTS idx_material_search_trgm "
    "ON materials (search_text gin_trgm_ops)",
    "CREATE INVERTED INDEX IF NOT EXISTS idx_vendor_search_trgm "
    "ON vendors (search_text gin_trgm_ops)",
    "CREATE INVERTED INDEX IF NOT EXISTS idx_material_attributes ON materials (attributes)",
)


def upgrade() -> None:
    bind = op.get_bind()
    models.Base.metadata.create_all(bind=bind)

    for table, column, index_name in VECTOR_INDEXED_TABLES:
        create_vector_index(bind, table=table, column=column, index_name=index_name)

    for statement in _SUPPLEMENTAL_INDEXES:
        try:
            op.execute(statement)
        except Exception:
            # Inverted indexes need a cluster feature that may be absent; the
            # application falls back to plain ILIKE scans.
            pass


def downgrade() -> None:
    models.Base.metadata.drop_all(bind=op.get_bind())
