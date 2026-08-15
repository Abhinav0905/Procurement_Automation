"""FastAPI dependencies: database session, authentication, service context."""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from procureguard.config import Settings, get_settings
from procureguard.domain.enums import Permission
from procureguard.infrastructure.db.session import get_session_factory
from procureguard.infrastructure.factory import ServiceContext
from procureguard.observability import new_correlation_id
from procureguard.security.auth import Authenticator, Principal


def get_db() -> Iterator[Session]:
    """Request-scoped session. Commits on success, rolls back on exception."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@lru_cache(maxsize=1)
def get_authenticator() -> Authenticator:
    return Authenticator(get_settings())


def get_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
    x_actor_id: Annotated[str | None, Header()] = None,
    x_actor_roles: Annotated[str | None, Header()] = None,
    x_tenant_id: Annotated[str | None, Header()] = None,
) -> Principal:
    principal = get_authenticator().authenticate(
        authorization=authorization or "",
        api_key=x_api_key or "",
        actor_header=x_actor_id or "",
        roles_header=x_actor_roles or "",
        tenant_header=x_tenant_id or "",
    )
    request.state.principal = principal
    return principal


def get_context(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    principal: Annotated[Principal, Depends(get_principal)],
) -> ServiceContext:
    correlation_id = getattr(request.state, "correlation_id", "") or new_correlation_id()
    return ServiceContext.build(
        db,
        tenant_id=principal.tenant_id,
        actor_id=principal.actor_id,
        actor_roles=tuple(principal.roles),
        correlation_id=correlation_id,
    )


def require(permission: Permission):
    """Route dependency enforcing one permission."""

    def _dependency(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        principal.require(permission)
        return principal

    return _dependency


def settings_dependency() -> Settings:
    return get_settings()


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
Context = Annotated[ServiceContext, Depends(get_context)]
Db = Annotated[Session, Depends(get_db)]
