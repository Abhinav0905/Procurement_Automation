"""Authentication and RBAC.

Three modes, chosen by `AUTH_MODE`:

* `oidc`   - production. Validates a JWT against the IdP's JWKS, maps group
             claims onto roles, and refuses tokens without an audience match.
* `static` - service-to-service API keys, for the Temporal worker and CI.
* `dev`    - local only. Trusts `X-Actor-Id`/`X-Actor-Roles` headers. `Settings`
             refuses to boot `prod` in this mode, so it cannot leak into an
             environment where it matters.

Authorisation itself lives in `domain.policies`; this module only establishes
*who* is calling.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any

from procureguard.config import Settings
from procureguard.domain.enums import Permission, Role
from procureguard.domain.errors import AuthenticationError, AuthorizationError
from procureguard.domain.policies import permissions_for_roles
from procureguard.observability import logger

log = logger(__name__)


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller."""

    actor_id: str
    email: str = ""
    display_name: str = ""
    roles: tuple[str, ...] = ()
    tenant_id: str = ""
    auth_method: str = "dev"
    subject: str = ""
    token_expires_at: int = 0
    claims: dict[str, Any] = field(default_factory=dict)

    @property
    def permissions(self) -> frozenset[Permission]:
        return permissions_for_roles(self.roles)

    @property
    def is_human(self) -> bool:
        return self.actor_id.upper() not in ("SYSTEM", "AGENT", "PROCUREGUARD", "WORKER")

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions

    def require(self, permission: Permission) -> None:
        if not self.has(permission):
            raise AuthorizationError(
                f"{self.actor_id} lacks {permission}",
                required=str(permission),
                roles=list(self.roles),
            )

    def require_human(self, action: str) -> None:
        if not self.is_human:
            raise AuthorizationError(
                f"{action} requires an authenticated human identity, not {self.actor_id}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor_id": self.actor_id,
            "email": self.email,
            "display_name": self.display_name,
            "roles": list(self.roles),
            "tenant_id": self.tenant_id,
            "auth_method": self.auth_method,
            "permissions": sorted(str(p) for p in self.permissions),
        }


SYSTEM_PRINCIPAL = Principal(
    actor_id="SYSTEM", display_name="ProcureGuard Agent", roles=(Role.SYSTEM.value,),
    auth_method="internal",
)


# Common IdP group -> role mappings. Overridable per deployment.
DEFAULT_GROUP_ROLE_MAP: dict[str, str] = {
    "procurement-requester": Role.REQUESTER.value,
    "procurement-buyer": Role.BUYER.value,
    "procurement-senior-buyer": Role.SENIOR_BUYER.value,
    "procurement-category-manager": Role.CATEGORY_MANAGER.value,
    "procurement-head": Role.PROCUREMENT_HEAD.value,
    "engineering": Role.ENGINEER.value,
    "quality": Role.QUALITY.value,
    "finance": Role.FINANCE.value,
    "executive": Role.EXECUTIVE.value,
    "audit": Role.AUDITOR.value,
    "procureguard-admin": Role.ADMIN.value,
}


def map_groups_to_roles(groups: list[str], mapping: dict[str, str] | None = None) -> tuple[str, ...]:
    mapping = mapping or DEFAULT_GROUP_ROLE_MAP
    roles: list[str] = []
    for group in groups:
        key = str(group).strip().lower()
        if key in mapping:
            roles.append(mapping[key])
            continue
        # Accept a raw role name too, so a deployment can skip group mapping.
        try:
            roles.append(Role(key.upper()).value)
        except ValueError:
            continue
    return tuple(dict.fromkeys(roles))


class Authenticator:
    """Resolves a request's credentials into a Principal."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._jwks: dict[str, Any] = {}
        self._jwks_fetched_at: float = 0.0
        self._static_keys = _parse_static_keys(settings.static_api_keys)

    def authenticate(
        self,
        *,
        authorization: str = "",
        api_key: str = "",
        actor_header: str = "",
        roles_header: str = "",
        tenant_header: str = "",
    ) -> Principal:
        tenant = tenant_header or self.settings.default_tenant_id

        if api_key and self._static_keys:
            principal = self._static_keys.get(_hash_key(api_key))
            if principal is None:
                raise AuthenticationError("Unrecognised API key")
            return Principal(**{**principal, "tenant_id": tenant})

        if authorization:
            token = authorization.removeprefix("Bearer ").removeprefix("bearer ").strip()
            if self.settings.auth_mode == "oidc":
                return self._from_oidc(token, tenant)
            if self.settings.auth_mode == "static":
                principal = self._static_keys.get(_hash_key(token))
                if principal is None:
                    raise AuthenticationError("Unrecognised bearer token")
                return Principal(**{**principal, "tenant_id": tenant})

        if self.settings.auth_mode == "dev":
            actor = actor_header.strip() or "dev.buyer"
            roles = tuple(r.strip().upper() for r in roles_header.split(",") if r.strip()) or (
                Role.BUYER.value,
            )
            return Principal(
                actor_id=actor,
                email=f"{actor}@local.test",
                display_name=actor,
                roles=roles,
                tenant_id=tenant,
                auth_method="dev",
            )

        raise AuthenticationError("No credentials supplied")

    # ------------------------------------------------------------------- OIDC
    def _from_oidc(self, token: str, tenant: str) -> Principal:
        header, payload = _decode_jwt_segments(token)
        self._verify_signature(token, header)

        now = int(time.time())
        if int(payload.get("exp", 0)) and int(payload["exp"]) < now:
            raise AuthenticationError("Token has expired")
        if int(payload.get("nbf", 0)) > now + 60:
            raise AuthenticationError("Token is not yet valid")
        if self.settings.oidc_issuer and payload.get("iss") != self.settings.oidc_issuer:
            raise AuthenticationError(
                f"Token issuer mismatch: {payload.get('iss')!r}"
            )
        if self.settings.oidc_audience:
            audience = payload.get("aud")
            audiences = audience if isinstance(audience, list) else [audience]
            if self.settings.oidc_audience not in audiences:
                raise AuthenticationError("Token audience mismatch")

        groups = payload.get("groups") or payload.get("roles") or payload.get("cognito:groups") or []
        if isinstance(groups, str):
            groups = [g.strip() for g in groups.split(",") if g.strip()]
        roles = map_groups_to_roles(list(groups))
        if not roles:
            raise AuthorizationError(
                "Token carries no group that maps to a ProcureGuard role",
                groups=list(groups),
            )
        return Principal(
            actor_id=str(payload.get("preferred_username") or payload.get("email") or payload["sub"]),
            email=str(payload.get("email", "")),
            display_name=str(payload.get("name", "")),
            roles=roles,
            tenant_id=str(payload.get("tenant_id") or tenant),
            auth_method="oidc",
            subject=str(payload.get("sub", "")),
            token_expires_at=int(payload.get("exp", 0)),
            claims=payload,
        )

    def _verify_signature(self, token: str, header: dict[str, Any]) -> None:
        """Verify against the IdP JWKS.

        RSA/EC verification needs a crypto library; when one is unavailable this
        refuses the token rather than accepting it unverified. An unverified JWT
        is worse than no JWT, because it looks like security.
        """
        jwks = self._load_jwks()
        kid = header.get("kid")
        key = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
        if key is None:
            raise AuthenticationError(f"No JWKS key matches kid={kid!r}")

        algorithm = header.get("alg", "")
        if algorithm in ("HS256", "HS384", "HS512"):
            secret = base64.urlsafe_b64decode(_pad(key["k"]))
            signing_input, signature = token.rsplit(".", 1)
            digest = {"HS256": hashlib.sha256, "HS384": hashlib.sha384, "HS512": hashlib.sha512}[
                algorithm
            ]
            expected = hmac.new(secret, signing_input.encode(), digest).digest()
            if not hmac.compare_digest(expected, base64.urlsafe_b64decode(_pad(signature))):
                raise AuthenticationError("Token signature is invalid")
            return

        try:
            import jwt  # type: ignore[import-not-found]
            from jwt import PyJWKClient  # type: ignore[import-not-found]
        except ImportError as exc:
            raise AuthenticationError(
                f"Signature algorithm {algorithm} requires PyJWT; "
                f"install 'procureguard[oidc]' to enable OIDC authentication"
            ) from exc

        signing_key = PyJWKClient(self.settings.oidc_jwks_url).get_signing_key_from_jwt(token)
        jwt.decode(
            token,
            signing_key.key,
            algorithms=[algorithm],
            audience=self.settings.oidc_audience or None,
            issuer=self.settings.oidc_issuer or None,
        )

    def _load_jwks(self) -> dict[str, Any]:
        if not self.settings.oidc_jwks_url:
            raise AuthenticationError("OIDC_JWKS_URL is not configured")
        age = time.time() - self._jwks_fetched_at
        if self._jwks and age < self.settings.oidc_jwks_cache_seconds:
            return self._jwks
        import urllib.request

        try:
            with urllib.request.urlopen(self.settings.oidc_jwks_url, timeout=10) as response:
                self._jwks = json.loads(response.read())
                self._jwks_fetched_at = time.time()
        except Exception as exc:
            if self._jwks:
                log.warning("jwks_refresh_failed_using_cache", detail=str(exc)[:200])
                return self._jwks
            raise AuthenticationError("Unable to fetch JWKS") from exc
        return self._jwks


def _decode_jwt_segments(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthenticationError("Malformed JWT")
    try:
        header = json.loads(base64.urlsafe_b64decode(_pad(parts[0])))
        payload = json.loads(base64.urlsafe_b64decode(_pad(parts[1])))
    except Exception as exc:
        raise AuthenticationError("JWT segments are not valid base64url JSON") from exc
    if header.get("alg", "").lower() == "none":
        raise AuthenticationError("Unsigned tokens are rejected")
    return header, payload


def _pad(segment: str) -> bytes:
    return (segment + "=" * (-len(segment) % 4)).encode()


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.strip().encode()).hexdigest()


def _parse_static_keys(spec: str) -> dict[str, dict[str, Any]]:
    """Parse "key:actor:ROLE1|ROLE2,key2:actor2:ROLE3" into a lookup."""
    out: dict[str, dict[str, Any]] = {}
    for entry in (spec or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) < 2:
            continue
        raw_key, actor = parts[0], parts[1]
        roles = tuple(r.strip().upper() for r in parts[2].split("|")) if len(parts) > 2 else (
            Role.SYSTEM.value,
        )
        out[_hash_key(raw_key)] = {
            "actor_id": actor,
            "display_name": actor,
            "roles": roles,
            "auth_method": "api_key",
        }
    return out
