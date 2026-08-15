"""Amazon Bedrock adapter.

Uses the Converse API so the same code targets any Bedrock-hosted model. Every
call is bounded (timeout, max tokens, retry budget), optionally routed through a
Bedrock Guardrail, and returns token counts and latency so the decision record
can state exactly what produced a recommendation.
"""

from __future__ import annotations

import json
import random
import time
from typing import Any

from procureguard.config import Settings
from procureguard.domain.errors import ExternalServiceError, ModelOutputError
from procureguard.observability import METRICS, logger
from procureguard.ports.services import ModelResponse

from .json_repair import extract_json, validate_shape

log = logger(__name__)

# Bedrock error codes that are worth another attempt.
_RETRYABLE = frozenset(
    {
        "ThrottlingException",
        "TooManyRequestsException",
        "ServiceUnavailableException",
        "InternalServerException",
        "ModelTimeoutException",
        "ModelNotReadyException",
    }
)


class BedrockLanguageModel:
    def __init__(self, settings: Settings, *, model_id: str = "") -> None:
        import boto3
        from botocore.config import Config

        self.settings = settings
        self.model_id = model_id or settings.bedrock_model_id
        if not self.model_id:
            raise ValueError("BEDROCK_MODEL_ID must be configured for llm_backend=bedrock")
        self.client = boto3.client(
            "bedrock-runtime",
            region_name=settings.aws_region,
            config=Config(
                read_timeout=settings.bedrock_timeout_seconds,
                connect_timeout=10,
                retries={"max_attempts": 1},  # retries are handled below
            ),
        )

    # ------------------------------------------------------------------ public
    def generate_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        purpose: str = "",
    ) -> ModelResponse:
        full_prompt = prompt
        if schema:
            full_prompt += (
                "\n\nReturn a single JSON document matching this schema. "
                "Output JSON only.\n" + json.dumps(schema, indent=2)
            )
        response = self._converse(
            system=system, prompt=full_prompt, max_tokens=max_tokens, purpose=purpose
        )
        try:
            payload = validate_shape(extract_json(response.raw_text), schema)
        except ModelOutputError:
            # One corrective retry: show the model its own broken output.
            METRICS.increment("llm.json_repair_retry", purpose=purpose or "unknown")
            retry = self._converse(
                system=system,
                prompt=(
                    full_prompt
                    + "\n\nYour previous response was not valid JSON:\n"
                    + response.raw_text[:2000]
                    + "\n\nReturn ONLY the corrected JSON document."
                ),
                max_tokens=max_tokens,
                purpose=f"{purpose}:repair",
            )
            payload = validate_shape(extract_json(retry.raw_text), schema)
            response = retry
        return ModelResponse(
            content=payload,
            model_id=response.model_id,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            latency_ms=response.latency_ms,
            stop_reason=response.stop_reason,
            guardrail_intervened=response.guardrail_intervened,
            raw_text=response.raw_text,
        )

    def generate_text(
        self, *, system: str, prompt: str, max_tokens: int | None = None, purpose: str = ""
    ) -> ModelResponse:
        response = self._converse(
            system=system, prompt=prompt, max_tokens=max_tokens, purpose=purpose
        )
        return ModelResponse(
            content=response.raw_text,
            model_id=response.model_id,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            latency_ms=response.latency_ms,
            stop_reason=response.stop_reason,
            guardrail_intervened=response.guardrail_intervened,
            raw_text=response.raw_text,
        )

    # ----------------------------------------------------------------- internal
    def _converse(
        self, *, system: str, prompt: str, max_tokens: int | None, purpose: str
    ) -> ModelResponse:
        kwargs: dict[str, Any] = {
            "modelId": self.model_id,
            "system": [{"text": system}],
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {
                "temperature": self.settings.bedrock_temperature,
                "maxTokens": max_tokens or self.settings.bedrock_max_tokens,
            },
        }
        if self.settings.bedrock_guardrail_id and self.settings.bedrock_guardrail_version:
            kwargs["guardrailConfig"] = {
                "guardrailIdentifier": self.settings.bedrock_guardrail_id,
                "guardrailVersion": self.settings.bedrock_guardrail_version,
                "trace": "enabled",
            }

        last_error: Exception | None = None
        for attempt in range(self.settings.bedrock_max_attempts):
            started = time.perf_counter()
            try:
                raw = self.client.converse(**kwargs)
            except Exception as exc:
                last_error = exc
                code = _error_code(exc)
                if code not in _RETRYABLE or attempt == self.settings.bedrock_max_attempts - 1:
                    raise ExternalServiceError(
                        f"Bedrock converse failed: {code}",
                        retryable=code in _RETRYABLE,
                        model_id=self.model_id,
                        purpose=purpose,
                        detail=str(exc)[:500],
                    ) from exc
                delay = min(20.0, (2**attempt) * 0.5) * (0.5 + random.random())
                log.warning("bedrock_retry", attempt=attempt + 1, code=code, delay=round(delay, 2))
                time.sleep(delay)
                continue

            latency_ms = int((time.perf_counter() - started) * 1000)
            usage = raw.get("usage", {})
            stop_reason = raw.get("stopReason", "")
            text = _first_text(raw)

            METRICS.increment("llm.calls", purpose=purpose or "unknown")
            METRICS.observe("llm.latency", latency_ms, purpose=purpose or "unknown")
            METRICS.increment(
                "llm.output_tokens", float(usage.get("outputTokens", 0)), purpose=purpose or "unknown"
            )

            if stop_reason == "guardrail_intervened":
                log.warning("bedrock_guardrail_intervened", purpose=purpose)
            if stop_reason == "max_tokens":
                log.warning("bedrock_truncated", purpose=purpose, latency_ms=latency_ms)

            return ModelResponse(
                content=text,
                model_id=self.model_id,
                input_tokens=int(usage.get("inputTokens", 0)),
                output_tokens=int(usage.get("outputTokens", 0)),
                latency_ms=latency_ms,
                stop_reason=stop_reason,
                guardrail_intervened=stop_reason == "guardrail_intervened",
                raw_text=text,
            )

        raise ExternalServiceError(
            "Bedrock converse exhausted retries", detail=str(last_error)[:500]
        )


class BedrockEmbeddingModel:
    """Titan / Cohere embeddings through Bedrock InvokeModel."""

    def __init__(self, settings: Settings) -> None:
        import boto3
        from botocore.config import Config

        self.settings = settings
        self._model_id = settings.bedrock_embedding_model_id
        self._dimensions = settings.embedding_dimensions
        self.client = boto3.client(
            "bedrock-runtime",
            region_name=settings.aws_region,
            config=Config(read_timeout=60, retries={"max_attempts": 3, "mode": "adaptive"}),
        )

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def model_id(self) -> str:
        return self._model_id

    def embed(self, text: str) -> list[float]:
        body: dict[str, Any] = {"inputText": (text or " ")[:40_000]}
        if "titan-embed-text-v2" in self._model_id:
            body["dimensions"] = self._dimensions
            body["normalize"] = True
        elif self._model_id.startswith("cohere."):
            body = {"texts": [(text or " ")[:40_000]], "input_type": "search_document"}
        try:
            raw = self.client.invoke_model(modelId=self._model_id, body=json.dumps(body))
            payload = json.loads(raw["body"].read())
        except Exception as exc:
            raise ExternalServiceError(
                f"Bedrock embedding failed for {self._model_id}", detail=str(exc)[:400]
            ) from exc

        vector = payload.get("embedding") or (payload.get("embeddings") or [[]])[0]
        if not vector:
            raise ExternalServiceError("Bedrock returned an empty embedding")
        if len(vector) != self._dimensions:
            raise ExternalServiceError(
                f"Embedding dimension mismatch: model returned {len(vector)}, "
                f"schema expects {self._dimensions}"
            )
        return [float(x) for x in vector]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(text) for text in texts]


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code", type(exc).__name__))
    return type(exc).__name__


def _first_text(raw: dict[str, Any]) -> str:
    blocks = raw.get("output", {}).get("message", {}).get("content", [])
    for block in blocks:
        if "text" in block:
            return str(block["text"])
    return ""
