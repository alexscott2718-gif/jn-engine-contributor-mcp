"""Shared contributor principal and REST authorization mapping."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastmcp.server.auth import AccessToken


class AuthDecisionState(StrEnum):
    ALLOWED = "allowed"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    AUTH_DEPENDENCY_UNAVAILABLE = "auth_dependency_unavailable"


@dataclass(frozen=True)
class Principal:
    provider: str
    subject: str
    login: str
    auth_mode: str


@dataclass(frozen=True)
class AuthDecision:
    state: AuthDecisionState
    principal: Principal | None = None
    access_token: AccessToken | None = field(default=None, repr=False)


class ContributorAuthenticator(Protocol):
    async def authenticate_and_authorize(self, token: str) -> AuthDecision:
        """Validate one gateway bearer and return a closed decision."""


_bearer = HTTPBearer(auto_error=False)


def build_rest_principal_dependency(authenticator: ContributorAuthenticator):
    """Bind protected REST routes to the exact authenticator used by MCP."""

    async def require_contributor(
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    ) -> Principal:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        decision = await authenticator.authenticate_and_authorize(
            credentials.credentials
        )
        if decision.state is AuthDecisionState.UNAUTHENTICATED:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if decision.state is AuthDecisionState.FORBIDDEN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="repository collaborator access is required",
            )
        if decision.state is AuthDecisionState.AUTH_DEPENDENCY_UNAVAILABLE:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="contributor authorization is temporarily unavailable",
            )
        if decision.principal is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="contributor authorization is temporarily unavailable",
            )
        return decision.principal

    return require_contributor


def build_local_principal_dependency():
    """Return a fixed principal for explicitly loopback-only local development."""

    async def require_local_contributor() -> Principal:
        return Principal(
            provider="local",
            subject="local:developer",
            login="local",
            auth_mode="authless_local",
        )

    return require_local_contributor
