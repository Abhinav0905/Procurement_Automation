"""Coercing model output into valid, schema-shaped JSON.

Models return JSON wrapped in prose or fences more often than anyone would like.
Repair is strictly syntactic - it never invents field values - and validation is
structural, so a malformed response fails loudly instead of silently producing a
half-populated evaluation.
"""

from __future__ import annotations

import json
import re
from typing import Any

from procureguard.domain.errors import ModelOutputError

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Pull the first complete JSON value out of a model response."""
    if text is None:
        raise ModelOutputError("Model returned no content")
    raw = text.strip()
    if not raw:
        raise ModelOutputError("Model returned empty content")

    for candidate in _candidates(raw):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            try:
                return json.loads(_repair(candidate))
            except json.JSONDecodeError:
                continue
    raise ModelOutputError(
        "Model response did not contain parseable JSON", excerpt=raw[:500], retryable=True
    )


def _candidates(raw: str) -> list[str]:
    out: list[str] = []
    fenced = _FENCE.search(raw)
    if fenced:
        out.append(fenced.group(1).strip())
    out.append(raw)
    for opener, closer in (("{", "}"), ("[", "]")):
        start = raw.find(opener)
        end = raw.rfind(closer)
        if start != -1 and end > start:
            out.append(raw[start : end + 1])
    # Longest first: a truncated inner object should lose to the full document.
    return sorted(dict.fromkeys(out), key=len, reverse=True)


def _repair(text: str) -> str:
    repaired = text.strip()
    repaired = re.sub(r"//[^\n\r]*", "", repaired)  # line comments
    repaired = re.sub(r"/\*.*?\*/", "", repaired, flags=re.DOTALL)  # block comments
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)  # trailing commas
    repaired = repaired.replace("“", '"').replace("”", '"')  # smart quotes
    repaired = repaired.replace("‘", "'").replace("’", "'")
    repaired = re.sub(r"\bNaN\b|\bInfinity\b|\b-Infinity\b", "null", repaired)
    repaired = re.sub(r"\bTrue\b", "true", repaired)
    repaired = re.sub(r"\bFalse\b", "false", repaired)
    repaired = re.sub(r"\bNone\b", "null", repaired)
    # Close unbalanced brackets caused by a truncated response.
    for opener, closer in (("{", "}"), ("[", "]")):
        deficit = repaired.count(opener) - repaired.count(closer)
        if deficit > 0:
            repaired += closer * deficit
    return repaired


def validate_shape(payload: Any, schema: dict[str, Any] | None) -> Any:
    """Minimal structural validation against a JSON-Schema-shaped hint.

    Deliberately not a full JSON Schema implementation: it checks the things
    that actually break downstream code - wrong root type, missing required
    keys, list-vs-object confusion - and lets everything else through.
    """
    if not schema:
        return payload

    expected = schema.get("type")
    if expected == "object" and not isinstance(payload, dict):
        raise ModelOutputError(
            f"Expected a JSON object, got {type(payload).__name__}", retryable=True
        )
    if expected == "array" and not isinstance(payload, list):
        raise ModelOutputError(
            f"Expected a JSON array, got {type(payload).__name__}", retryable=True
        )

    if isinstance(payload, dict):
        missing = [key for key in schema.get("required", []) if key not in payload]
        if missing:
            raise ModelOutputError(
                f"Model response is missing required fields: {missing}",
                missing=missing,
                retryable=True,
            )
        properties = schema.get("properties", {})
        for key, sub_schema in properties.items():
            if key in payload and isinstance(sub_schema, dict):
                sub_type = sub_schema.get("type")
                if sub_type == "array" and payload[key] is None:
                    payload[key] = []
                elif sub_type == "object" and payload[key] is None:
                    payload[key] = {}

    if isinstance(payload, list) and isinstance(schema.get("items"), dict):
        item_schema = schema["items"]
        for item in payload:
            validate_shape(item, item_schema)

    return payload


def as_decimal_str(value: Any) -> str:
    """Normalise a model-supplied number to a Decimal-safe string."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    text = str(value).strip().replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return match.group(0) if match else ""
