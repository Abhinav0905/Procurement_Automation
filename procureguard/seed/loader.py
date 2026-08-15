"""Bulk loader.

Row-by-row INSERT would take hours for a million PO lines. This streams through
`COPY ... FROM STDIN`, which is the fastest supported path into CockroachDB
short of IMPORT, and batches so that a single transaction never grows large
enough to hit the cluster's transaction size limit.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from procureguard.observability import logger

log = logger(__name__)

# CockroachDB rejects transactions above roughly 64 MiB of intents. Batching
# keeps each COPY well under that while still amortising round trips.
DEFAULT_BATCH = 5_000


class BulkLoader:
    def __init__(self, engine: Engine, *, batch_size: int = DEFAULT_BATCH) -> None:
        self.engine = engine
        self.batch_size = batch_size

    def copy_rows(
        self, table: str, columns: list[str], rows: Iterable[dict[str, Any]]
    ) -> int:
        """COPY an iterable of dicts into a table. Returns rows written."""
        total = 0
        column_list = ", ".join(columns)
        statement = f"COPY {table} ({column_list}) FROM STDIN"

        for batch in _batched(rows, self.batch_size):
            if not batch:
                continue
            raw = self.engine.raw_connection()
            try:
                with raw.cursor() as cursor, cursor.copy(statement) as copy:
                    for row in batch:
                        copy.write_row(tuple(_adapt(row.get(c)) for c in columns))
                raw.commit()
                total += len(batch)
            except Exception:
                raw.rollback()
                raise
            finally:
                raw.close()
            if total % (self.batch_size * 10) == 0:
                log.info("bulk_load_progress", table=table, rows=total)
        if total:
            log.info("bulk_load_complete", table=table, rows=total)
        return total

    def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> int:
        """Fallback path for small tables, using multi-row INSERT."""
        if not rows:
            return 0
        columns = list(rows[0].keys())
        placeholders = ", ".join(f":{c}" for c in columns)
        statement = text(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
        )
        written = 0
        with self.engine.begin() as conn:
            for batch in _batched(iter(rows), 500):
                conn.execute(statement, [{k: _adapt(v) for k, v in r.items()} for r in batch])
                written += len(batch)
        return written

    def truncate(self, tables: list[str]) -> None:
        """Wipe tables in dependency-safe order."""
        with self.engine.begin() as conn:
            for table in tables:
                try:
                    conn.execute(text(f"DELETE FROM {table} WHERE 1=1"))
                except Exception as exc:
                    log.warning("truncate_failed", table=table, detail=str(exc)[:200])

    def analyze(self, tables: list[str]) -> None:
        """Refresh statistics so the optimiser picks the right indexes.

        Without this, CockroachDB plans against stale statistics right after a
        bulk load and a benchmark query that should use idx_ph_material_date can
        end up scanning.
        """
        with self.engine.begin() as conn:
            for table in tables:
                try:
                    conn.execute(text(f"ANALYZE {table}"))
                    log.info("statistics_refreshed", table=table)
                except Exception as exc:
                    log.info("analyze_skipped", table=table, detail=str(exc)[:160])


def _adapt(value: Any) -> Any:
    """Convert Python values into something psycopg can COPY."""
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple)):
        # JSONB columns: psycopg needs an explicit JSON wrapper in COPY.
        from psycopg.types.json import Jsonb

        return Jsonb(_jsonable(value))
    if isinstance(value, bool | int | float | Decimal | str | datetime):
        return value
    if hasattr(value, "isoformat"):
        return value
    return str(value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _batched(iterable: Iterable[Any], size: int) -> Iterator[list[Any]]:
    batch: list[Any] = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def vector_literal(vector: list[float] | None, native: bool) -> Any:
    """Format an embedding for whichever column type the schema chose."""
    if vector is None:
        return None
    if native:
        return "[" + ",".join(format(float(x), ".6g") for x in vector) + "]"
    return list(vector)


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


def json_or_none(value: Any) -> Any:
    return json.dumps(value) if value is not None else None
