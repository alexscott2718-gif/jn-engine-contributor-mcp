"""GitHub OAuth identity plus fail-closed repository collaborator authorization."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote

import httpx
from cryptography.fernet import Fernet
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.jwt_issuer import derive_jwt_key
from fastmcp.server.auth.providers.github import GitHubProvider
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from app.auth import AuthDecision, AuthDecisionState, Principal
from app.config import (
    EXPECTED_REPOSITORY,
    GITHUB_API_BASE_URL,
    GITHUB_API_VERSION,
    GITHUB_OAUTH_CALLBACK_PATH,
    GITHUB_OAUTH_SCOPE,
    Settings,
)

logger = logging.getLogger(__name__)

_GITHUB_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_COLLABORATOR_PATH = (
    "/repos/alexscott2718-gif/jn-engine/collaborators/{login}"
)
_USER_AGENT = "jn-engine-contributor-mcp/0.1"


class CollaboratorState(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class CollaboratorDecision:
    state: CollaboratorState
    status_code: int | None
    reason: str


@dataclass(frozen=True)
class _CacheEntry:
    state: CollaboratorState
    status_code: int
    expires_at: float


class ContributorAuthorizer:
    """Check a verified GitHub identity against one fixed repository."""

    def __init__(
        self,
        *,
        server_token: str,
        positive_ttl_seconds: int = 300,
        negative_ttl_seconds: int = 60,
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not server_token:
            raise ValueError("GitHub collaborator server token is required")
        self._positive_ttl_seconds = positive_ttl_seconds
        self._negative_ttl_seconds = negative_ttl_seconds
        self._clock = clock
        self._cache: dict[tuple[str, str], _CacheEntry] = {}
        self._lock = asyncio.Lock()
        self._owns_client = client is None
        self._request_headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {server_token}",
            "User-Agent": _USER_AGENT,
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        self._client = client or httpx.AsyncClient(
            base_url=GITHUB_API_BASE_URL,
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
        )

    async def _request(self, login: str) -> CollaboratorDecision:
        path = _COLLABORATOR_PATH.format(login=quote(login, safe=""))
        try:
            response = await self._client.get(path, headers=self._request_headers)
        except httpx.RequestError:
            logger.warning("GitHub collaborator check unavailable: request_error")
            return CollaboratorDecision(
                CollaboratorState.UNAVAILABLE,
                None,
                "request_error",
            )
        except Exception:
            logger.warning(
                "GitHub collaborator check unavailable: unexpected_client_error"
            )
            return CollaboratorDecision(
                CollaboratorState.UNAVAILABLE,
                None,
                "unexpected_client_error",
            )

        if response.status_code == 204:
            return CollaboratorDecision(
                CollaboratorState.ALLOWED,
                204,
                "github",
            )
        if response.status_code == 404:
            return CollaboratorDecision(
                CollaboratorState.DENIED,
                404,
                "github",
            )
        logger.warning(
            "GitHub collaborator check unavailable: status=%d",
            response.status_code,
        )
        return CollaboratorDecision(
            CollaboratorState.UNAVAILABLE,
            response.status_code,
            "upstream_status",
        )

    async def check(self, *, user_id: str, login: str) -> CollaboratorDecision:
        normalized_login = login.casefold()
        if (
            not user_id.isdigit()
            or int(user_id) <= 0
            or not _GITHUB_LOGIN.fullmatch(login)
        ):
            return CollaboratorDecision(
                CollaboratorState.UNAVAILABLE,
                None,
                "invalid_verified_identity",
            )

        cache_key = (user_id, normalized_login)
        async with self._lock:
            now = self._clock()
            cached = self._cache.get(cache_key)
            if cached is not None and cached.expires_at > now:
                return CollaboratorDecision(cached.state, cached.status_code, "cache")
            self._cache.pop(cache_key, None)

            decision = await self._request(login)
            if decision.state is CollaboratorState.UNAVAILABLE:
                return decision
            state = decision.state
            ttl = (
                self._positive_ttl_seconds
                if state is CollaboratorState.ALLOWED
                else self._negative_ttl_seconds
            )

            self._cache[cache_key] = _CacheEntry(
                state=state,
                status_code=decision.status_code or 0,
                expires_at=now + ttl,
            )
            return decision

    async def preflight(self, *, login: str) -> CollaboratorDecision:
        """Perform an uncached deployment check for one known collaborator."""
        if not _GITHUB_LOGIN.fullmatch(login):
            return CollaboratorDecision(
                CollaboratorState.UNAVAILABLE,
                None,
                "invalid_login",
            )
        async with self._lock:
            return await self._request(login)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _encrypted_oauth_store(
    *,
    directory: Path,
    jwt_source_material: str,
):
    directory.mkdir(mode=0o700, parents=False, exist_ok=True)
    metadata = directory.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("FastMCP OAuth state path must be a real directory")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValueError("FastMCP OAuth state directory must have mode 0700")

    jwt_key = derive_jwt_key(
        low_entropy_material=jwt_source_material,
        salt="fastmcp-jwt-signing-key",
    )
    storage_key = derive_jwt_key(
        high_entropy_material=jwt_key.decode("ascii"),
        salt="fastmcp-storage-encryption-key",
    )
    return FernetEncryptionWrapper(
        key_value=FileTreeStore(
            data_directory=directory,
            collection_sanitization_strategy=(
                FileTreeV1CollectionSanitizationStrategy(directory)
            ),
            key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(directory),
        ),
        fernet=Fernet(storage_key),
    )


class ContributorGitHubProvider(GitHubProvider):
    """FastMCP GitHub provider extended by one shared authorization decision."""

    def __init__(
        self,
        *,
        settings: Settings,
        authorizer: ContributorAuthorizer,
    ) -> None:
        if settings.auth_mode != "github":
            raise ValueError("ContributorGitHubProvider requires AUTH_MODE=github")
        # This service writes only encrypted OAuth state. Keep current and future
        # file-tree entries private, including files created after initialization.
        os.umask(0o077)
        jwt_source_material = base64.urlsafe_b64encode(
            settings.oauth_jwt_signing_key()
        ).decode("ascii")
        client_storage = _encrypted_oauth_store(
            directory=settings.gateway_secrets_dir / "oauth-proxy",
            jwt_source_material=jwt_source_material,
        )
        super().__init__(
            client_id=settings.github_oauth_client_id,
            client_secret=settings.github_oauth_client_secret(),
            base_url=settings.public_base_url,
            issuer_url=settings.public_base_url,
            redirect_path=GITHUB_OAUTH_CALLBACK_PATH,
            required_scopes=[GITHUB_OAUTH_SCOPE],
            timeout_seconds=10,
            allowed_client_redirect_uris=list(
                settings.oauth_allowed_client_redirect_uris
            ),
            client_storage=client_storage,
            jwt_signing_key=jwt_source_material,
            require_authorization_consent=True,
        )
        self._contributor_authorizer = authorizer

    async def authenticate_and_authorize(self, token: str) -> AuthDecision:
        identity = await super().verify_token(token)
        if identity is None:
            return AuthDecision(AuthDecisionState.UNAUTHENTICATED)

        raw_user_id = identity.claims.get("sub")
        raw_login = identity.claims.get("login")
        if not isinstance(raw_user_id, str) or not isinstance(raw_login, str):
            return AuthDecision(AuthDecisionState.UNAUTHENTICATED)

        collaborator = await self._contributor_authorizer.check(
            user_id=raw_user_id,
            login=raw_login,
        )
        if collaborator.state is CollaboratorState.DENIED:
            return AuthDecision(AuthDecisionState.FORBIDDEN)
        if collaborator.state is CollaboratorState.UNAVAILABLE:
            return AuthDecision(AuthDecisionState.AUTH_DEPENDENCY_UNAVAILABLE)

        principal = Principal(
            provider="github",
            subject=f"github:{raw_user_id}",
            login=raw_login,
            auth_mode="github",
        )
        return AuthDecision(
            AuthDecisionState.ALLOWED,
            principal=principal,
            access_token=identity,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        """MCP deliberately collapses forbidden/unavailable into invalid_token."""
        decision = await self.authenticate_and_authorize(token)
        if decision.state is AuthDecisionState.ALLOWED:
            return decision.access_token
        return None


def create_github_provider(
    settings: Settings,
) -> tuple[ContributorGitHubProvider, ContributorAuthorizer]:
    """Build the singleton provider/authorizer pair used by both surfaces."""
    if settings.expected_repository != EXPECTED_REPOSITORY:
        raise ValueError("repository assertion mismatch")
    authorizer = ContributorAuthorizer(
        server_token=settings.github_collaborator_token(),
        positive_ttl_seconds=settings.github_collab_cache_ttl_seconds,
        negative_ttl_seconds=settings.github_collab_negative_ttl_seconds,
    )
    provider = ContributorGitHubProvider(
        settings=settings,
        authorizer=authorizer,
    )
    return provider, authorizer
