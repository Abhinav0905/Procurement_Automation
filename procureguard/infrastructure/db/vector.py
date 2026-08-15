"""Vector storage and similarity search.

CockroachDB v25.2+ ships a native ``VECTOR(n)`` type with C-SPANN ANN indexes and
pgvector-compatible distance operators. Older CockroachDB, and vanilla Postgres
without pgvector, do not. Rather than fork the schema, the embedding column is a
single TypeDecorator whose DDL is chosen from a capability probe performed once
when the engine is created:

* native  -> ``VECTOR(n)`` + ``<=>`` cosine distance, index-accelerated in SQL
* json    -> ``JSONB`` array + exact cosine ranked over a bounded candidate set

Both paths return the same ordered results; only latency at scale differs.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.types import TypeDecorator, UserDefinedType

from procureguard.observability import logger

log = logger(__name__)

# Resolved once per process by probe_vector_support(); models read it at DDL time.
_NATIVE_VECTOR_SUPPORTED: bool = False
_PROBED: bool = False


def native_vector_enabled() -> bool:
    return _NATIVE_VECTOR_SUPPORTED


def set_vector_mode(native: bool) -> None:
    global _NATIVE_VECTOR_SUPPORTED, _PROBED
    _NATIVE_VECTOR_SUPPORTED = native
    _PROBED = True


def probe_vector_support(engine: Engine, *, force: bool = False) -> bool:
    """Ask the server whether a VECTOR column can actually be created.

    Any failure means "no": a degraded-but-working JSONB path always beats a
    startup crash on an older cluster.
    """
    global _PROBED
    if _PROBED and not force:
        return _NATIVE_VECTOR_SUPPORTED
    supported = False
    try:
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS _pg_vector_probe (v VECTOR(3))"))
            conn.execute(text("DROP TABLE IF EXISTS _pg_vector_probe"))
            conn.commit()
            supported = True
    except Exception as exc:  # pragma: no cover - depends on server build
        log.info("native_vector_unavailable", detail=str(exc)[:200])
        supported = False
    set_vector_mode(supported)
    log.info("vector_backend_selected", backend="native" if supported else "json")
    return supported


class _NativeVector(UserDefinedType):
    """Renders as ``VECTOR(n)``; binds/returns a pgvector-style literal."""

    cache_ok = True

    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions

    def get_col_spec(self, **_: Any) -> str:
        return f"VECTOR({self.dimensions})"

    def bind_processor(self, dialect: Any):  # noqa: ANN201
        def process(value: Any) -> str | None:
            if value is None:
                return None
            return "[" + ",".join(format(float(x), ".7g") for x in value) + "]"

        return process

    def result_processor(self, dialect: Any, coltype: Any):  # noqa: ANN201
        def process(value: Any) -> list[float] | None:
            if value is None:
                return None
            if isinstance(value, (list, tuple)):
                return [float(x) for x in value]
            return [float(x) for x in str(value).strip("[]").split(",") if x.strip()]

        return process


class EmbeddingVector(TypeDecorator):
    """Portable embedding column: native VECTOR when available, JSONB otherwise."""

    impl = JSONB
    cache_ok = True

    def __init__(self, dimensions: int = 1024) -> None:
        self.dimensions = dimensions
        super().__init__()

    def load_dialect_impl(self, dialect: Any):  # noqa: ANN201
        if _NATIVE_VECTOR_SUPPORTED:
            return dialect.type_descriptor(_NativeVector(self.dimensions))
        return dialect.type_descriptor(JSONB())

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        vector = [float(x) for x in value]
        if len(vector) != self.dimensions:
            raise ValueError(
                f"Embedding has {len(vector)} dimensions, expected {self.dimensions}"
            )
        if _NATIVE_VECTOR_SUPPORTED:
            return vector  # _NativeVector.bind_processor renders the literal
        return vector

    def process_result_value(self, value: Any, dialect: Any) -> list[float] | None:
        if value is None:
            return None
        if isinstance(value, str):
            try:
                return [float(x) for x in json.loads(value)]
            except (ValueError, TypeError):
                return [float(x) for x in value.strip("[]").split(",") if x.strip()]
        return [float(x) for x in value]


# --------------------------------------------------------------------- maths

def l2_normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(float(x) * float(x) for x in vector))
    if norm == 0.0:
        return [0.0] * len(vector)
    return [float(x) / norm for x in vector]


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=False):
        fx, fy = float(x), float(y)
        dot += fx * fy
        norm_a += fx * fx
        norm_b += fy * fy
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def to_vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(format(float(x), ".7g") for x in vector) + "]"


@dataclass(frozen=True, slots=True)
class VectorHit:
    row_id: str
    score: float  # cosine similarity in [-1, 1]; higher is more similar
    payload: dict[str, Any]


class VectorSearch:
    """KNN over an embedding column, with the same contract on both backends."""

    def __init__(self, *, dimensions: int, candidate_limit: int = 200) -> None:
        self.dimensions = dimensions
        self.candidate_limit = candidate_limit

    def search(
        self,
        conn: Connection,
        *,
        table: str,
        id_column: str,
        embedding_column: str,
        query_vector: Sequence[float],
        top_k: int = 10,
        where_sql: str = "",
        params: dict[str, Any] | None = None,
        payload_columns: Sequence[str] = (),
    ) -> list[VectorHit]:
        params = dict(params or {})
        selected = ", ".join([id_column, *payload_columns])
        predicate = f"WHERE {where_sql}" if where_sql else ""

        if _NATIVE_VECTOR_SUPPORTED:
            params["query_vec"] = to_vector_literal(query_vector)
            sql = text(
                f"""
                SELECT {selected},
                       1 - ({embedding_column} <=> CAST(:query_vec AS VECTOR({self.dimensions})))
                           AS score
                FROM {table}
                {predicate}
                ORDER BY {embedding_column} <=> CAST(:query_vec AS VECTOR({self.dimensions}))
                LIMIT :top_k
                """
            )
            params["top_k"] = top_k
            rows = conn.execute(sql, params).mappings().all()
            return [
                VectorHit(
                    row_id=str(row[id_column]),
                    score=float(row["score"]),
                    payload={c: row[c] for c in payload_columns},
                )
                for row in rows
            ]

        # JSONB fallback: pull a bounded candidate set and rank exactly in Python.
        sql = text(
            f"""
            SELECT {selected}, {embedding_column} AS _embedding
            FROM {table}
            {predicate}
            LIMIT :candidate_limit
            """
        )
        params["candidate_limit"] = self.candidate_limit
        rows = conn.execute(sql, params).mappings().all()
        scored: list[VectorHit] = []
        for row in rows:
            raw = row["_embedding"]
            if raw is None:
                continue
            vector = json.loads(raw) if isinstance(raw, str) else raw
            scored.append(
                VectorHit(
                    row_id=str(row[id_column]),
                    score=cosine_similarity(query_vector, vector),
                    payload={c: row[c] for c in payload_columns},
                )
            )
        scored.sort(key=lambda hit: hit.score, reverse=True)
        return scored[:top_k]


def create_vector_index(
    conn: Connection, *, table: str, column: str, index_name: str
) -> bool:
    """Create an ANN index when the cluster supports one. Never fatal."""
    if not _NATIVE_VECTOR_SUPPORTED:
        return False
    try:
        conn.execute(text(f"CREATE VECTOR INDEX IF NOT EXISTS {index_name} ON {table} ({column})"))
        return True
    except Exception as exc:  # pragma: no cover - depends on cluster licence/build
        log.info("vector_index_skipped", table=table, detail=str(exc)[:200])
        return False


__all__ = [
    "EmbeddingVector",
    "VectorHit",
    "VectorSearch",
    "cosine_similarity",
    "create_vector_index",
    "l2_normalize",
    "native_vector_enabled",
    "probe_vector_support",
    "set_vector_mode",
    "to_vector_literal",
]
