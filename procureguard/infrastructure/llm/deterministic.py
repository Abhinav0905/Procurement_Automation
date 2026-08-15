"""Credential-free language-model stand-in.

Architectural note, because this is easy to misread as a mock: every extraction
stage in ProcureGuard runs a deterministic rule-based parser **first** and treats
the language model as a *supplement* that fills gaps the parser could not. This
adapter is the supplement returning "nothing further to add".

The consequence is the property we actually want: with `LLM_BACKEND=deterministic`
the full fifteen-stage pipeline still runs end to end, in CI, with no AWS
account - and the outputs are the deterministic parser's, which are exactly the
outputs a reviewer can reproduce by hand.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from procureguard.config import Settings
from procureguard.observability import METRICS, logger
from procureguard.ports.services import ModelResponse

log = logger(__name__)

MODEL_ID = "procureguard-deterministic-v1"

# Schema-valid empty supplements, keyed by the caller's declared purpose.
_EMPTY_BY_PURPOSE: dict[str, dict[str, Any]] = {
    "requirement_extraction": {"requirements": []},
    "quotation_extraction": {"lines": [], "technical_answers": []},
    "compliance_extraction": {"answers": []},
    "pr_parse": {"lines": []},
    "negotiation_strategy": {"talking_points": [], "non_price_asks": [], "risk_notes": []},
}


class DeterministicLanguageModel:
    """Returns well-formed, empty supplements. Never fabricates a value."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings
        self.model_id = MODEL_ID
        self.calls: list[dict[str, str]] = []  # inspectable in tests

    def generate_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        purpose: str = "",
    ) -> ModelResponse:
        started = time.perf_counter()
        self.calls.append({"purpose": purpose, "prompt_digest": _digest(prompt)})
        METRICS.increment("llm.calls", purpose=purpose or "unknown", backend="deterministic")

        payload = _EMPTY_BY_PURPOSE.get(purpose)
        if payload is None:
            payload = _empty_for_schema(schema)
        return ModelResponse(
            content=payload,
            model_id=self.model_id,
            latency_ms=int((time.perf_counter() - started) * 1000),
            stop_reason="end_turn",
            raw_text="",
        )

    def generate_text(
        self, *, system: str, prompt: str, max_tokens: int | None = None, purpose: str = ""
    ) -> ModelResponse:
        started = time.perf_counter()
        self.calls.append({"purpose": purpose, "prompt_digest": _digest(prompt)})
        METRICS.increment("llm.calls", purpose=purpose or "unknown", backend="deterministic")
        # Callers that need prose always have a template fallback; returning an
        # empty string makes them use it rather than emitting placeholder text
        # into a supplier-facing document.
        return ModelResponse(
            content="",
            model_id=self.model_id,
            latency_ms=int((time.perf_counter() - started) * 1000),
            stop_reason="end_turn",
            raw_text="",
        )


class HashingEmbeddingModel:
    """Deterministic embeddings from hashed word and character n-grams.

    Not a semantic model - it captures lexical overlap - but it is stable across
    processes, needs no network, and is genuinely useful for the matching this
    system does (part descriptions, supplier capability text). Swapping in
    `BedrockEmbeddingModel` changes only recall quality, never the schema, since
    both emit the same dimensionality.
    """

    def __init__(self, dimensions: int = 1024) -> None:
        self._dimensions = int(dimensions)
        self._model_id = f"procureguard-hashing-{self._dimensions}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def model_id(self) -> str:
        return self._model_id

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self._dimensions
        normalized = _normalize(text)
        if not normalized:
            return vector

        words = normalized.split()
        for word in words:
            _accumulate(vector, f"w:{word}", 1.0, self._dimensions)
        for a, b in zip(words, words[1:], strict=False):
            _accumulate(vector, f"b:{a}_{b}", 0.7, self._dimensions)
        # Character trigrams give partial-token matching, which is what makes
        # "M8x40 hex bolt" retrieve "hex bolt M8 x 40mm".
        compact = normalized.replace(" ", "")
        for i in range(len(compact) - 2):
            _accumulate(vector, f"c:{compact[i : i + 3]}", 0.35, self._dimensions)

        norm = sum(x * x for x in vector) ** 0.5
        if norm == 0.0:
            return vector
        return [x / norm for x in vector]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


def _accumulate(vector: list[float], token: str, weight: float, dimensions: int) -> None:
    digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
    index = int.from_bytes(digest[:4], "big") % dimensions
    # Signed hashing keeps unrelated tokens from piling up in one direction.
    sign = 1.0 if digest[4] & 1 else -1.0
    vector[index] += weight * sign


def _normalize(text: str) -> str:
    lowered = (text or "").lower()
    cleaned = "".join(ch if ch.isalnum() else " " for ch in lowered)
    return " ".join(cleaned.split())


def _digest(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()[:16]


def _empty_for_schema(schema: dict[str, Any] | None) -> Any:
    if not schema:
        return {}
    if schema.get("type") == "array":
        return []
    payload: dict[str, Any] = {}
    for key in schema.get("required", []):
        sub = schema.get("properties", {}).get(key, {})
        sub_type = sub.get("type") if isinstance(sub, dict) else None
        payload[key] = [] if sub_type == "array" else ({} if sub_type == "object" else None)
    return payload
