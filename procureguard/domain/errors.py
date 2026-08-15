"""Domain error hierarchy.

The API layer maps these onto HTTP status codes in `procureguard.api.errors`, so
services never need to know about HTTP.
"""

from __future__ import annotations

from typing import Any


class ProcureGuardError(Exception):
    """Base class for every error this system raises deliberately."""

    code = "PROCUREGUARD_ERROR"

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = context

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "context": self.context}


class DomainInvariantError(ProcureGuardError):
    code = "DOMAIN_INVARIANT"


class UnsafeTransitionError(DomainInvariantError):
    code = "UNSAFE_TRANSITION"


class NotFoundError(ProcureGuardError):
    code = "NOT_FOUND"


class ConflictError(ProcureGuardError):
    code = "CONFLICT"


class ValidationError(ProcureGuardError):
    code = "VALIDATION_FAILED"


class AuthorizationError(ProcureGuardError):
    code = "FORBIDDEN"


class AuthenticationError(ProcureGuardError):
    code = "UNAUTHENTICATED"


class PolicyViolationError(ProcureGuardError):
    """A deterministic guardrail refused an action the agent proposed."""

    code = "POLICY_VIOLATION"


class SecurityQuarantineError(ProcureGuardError):
    """Supplier-controlled content failed the document firewall."""

    code = "SECURITY_QUARANTINE"


class SealedBidError(ProcureGuardError):
    """Commercial data was requested before technical approval unlocked it."""

    code = "SEALED_BID_LOCKED"


class ExternalServiceError(ProcureGuardError):
    """A remote dependency failed. Usually retryable."""

    code = "EXTERNAL_SERVICE_ERROR"

    def __init__(self, message: str, *, retryable: bool = True, **context: Any) -> None:
        super().__init__(message, **context)
        self.retryable = retryable


class ModelOutputError(ExternalServiceError):
    """The language model returned output that failed schema validation."""

    code = "MODEL_OUTPUT_INVALID"


class UnitConversionError(ValidationError):
    code = "UNIT_CONVERSION_FAILED"


class CurrencyConversionError(ValidationError):
    code = "CURRENCY_CONVERSION_FAILED"
