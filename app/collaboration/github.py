"""Read-only GitHub REST client with bounded transient retries."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from app.collaboration.errors import (
    credential_unavailable,
    not_found,
    upstream_unavailable,
)
from app.config import GITHUB_API_BASE_URL, GITHUB_API_VERSION

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (0.25, 0.75)


class GitHubReadSession:
    """One credential and status trail shared by a single tool call."""

    def __init__(
        self,
        client: httpx.Client,
        token: str,
        *,
        sleeper: Callable[[float], None],
    ) -> None:
        self._client = client
        self._token = token
        self._sleep = sleeper
        self.statuses: list[int] = []

    @staticmethod
    def _rate_limited(response: httpx.Response) -> bool:
        if response.status_code == 429:
            return True
        if response.status_code != 403:
            return False
        message = response.text.casefold()
        return (
            "retry-after" in response.headers
            or response.headers.get("x-ratelimit-remaining") == "0"
            or "secondary rate limit" in message
            or "rate limit exceeded" in message
        )

    def _delay(self, response: httpx.Response | None, attempt: int) -> None:
        retry_after = response.headers.get("retry-after") if response else None
        if retry_after is not None:
            try:
                self._sleep(min(max(float(retry_after), 0.0), 5.0))
                return
            except (OverflowError, ValueError):
                pass
        self._sleep(BACKOFF_SECONDS[attempt])

    def get(
        self,
        path: str,
        *,
        params: dict[str, object] | None = None,
        not_found_is_credential: bool = False,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "jn-engine-contributor-mcp",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        for attempt in range(MAX_ATTEMPTS):
            response: httpx.Response | None = None
            try:
                response = self._client.get(path, params=params, headers=headers)
            except httpx.RequestError as exc:
                if attempt == MAX_ATTEMPTS - 1:
                    raise upstream_unavailable() from exc
                self._delay(None, attempt)
                continue

            self.statuses.append(response.status_code)
            if 200 <= response.status_code < 300:
                try:
                    body = response.json()
                except ValueError as exc:
                    raise upstream_unavailable() from exc
                if not isinstance(body, dict):
                    raise upstream_unavailable()
                return body
            if response.status_code == 401:
                raise credential_unavailable()
            if response.status_code == 404:
                if not_found_is_credential:
                    raise credential_unavailable()
                raise not_found()
            transient = response.status_code >= 500 or self._rate_limited(response)
            if transient:
                if attempt == MAX_ATTEMPTS - 1:
                    raise upstream_unavailable()
                self._delay(response, attempt)
                continue
            if response.status_code == 403:
                raise credential_unavailable()
            raise upstream_unavailable()
        raise upstream_unavailable()


class GitHubReadClient:
    def __init__(
        self,
        token_loader: Callable[[], str],
        *,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._token_loader = token_loader
        self._client = client or httpx.Client(
            base_url=GITHUB_API_BASE_URL,
            timeout=httpx.Timeout(10.0),
        )
        self._owns_client = client is None
        self._sleeper = sleeper

    def begin(self) -> GitHubReadSession:
        try:
            token = self._token_loader()
        except (OSError, ValueError) as exc:
            raise credential_unavailable() from exc
        if not token:
            raise credential_unavailable()
        return GitHubReadSession(self._client, token, sleeper=self._sleeper)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class GitHubWriteSession(GitHubReadSession):
    """One PR-write credential and status trail for a single open_pr call.

    Safe GETs inherit the bounded read retries. Mutating requests are sent
    exactly once: after a mutation may have been applied upstream, a retry can
    silently duplicate work, so any transport error or unexpected status fails
    the whole call closed instead.
    """

    _MUTATION_HANDLED_DEFAULT: tuple[int, ...] = ()

    def get_list(
        self,
        path: str,
        *,
        params: dict[str, object] | None = None,
    ) -> list[Any]:
        """Bounded-retry GET for endpoints whose body is a JSON array."""
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "jn-engine-contributor-mcp",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        for attempt in range(MAX_ATTEMPTS):
            response: httpx.Response | None = None
            try:
                response = self._client.get(path, params=params, headers=headers)
            except httpx.RequestError as exc:
                if attempt == MAX_ATTEMPTS - 1:
                    raise upstream_unavailable() from exc
                self._delay(None, attempt)
                continue
            self.statuses.append(response.status_code)
            if 200 <= response.status_code < 300:
                try:
                    body = response.json()
                except ValueError as exc:
                    raise upstream_unavailable() from exc
                if not isinstance(body, list):
                    raise upstream_unavailable()
                return body
            if response.status_code == 401:
                raise credential_unavailable()
            if response.status_code == 404:
                raise not_found()
            transient = response.status_code >= 500 or self._rate_limited(response)
            if transient:
                if attempt == MAX_ATTEMPTS - 1:
                    raise upstream_unavailable()
                self._delay(response, attempt)
                continue
            if response.status_code == 403:
                raise credential_unavailable()
            raise upstream_unavailable()
        raise upstream_unavailable()

    def send(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any],
        handled: tuple[int, ...] = _MUTATION_HANDLED_DEFAULT,
    ) -> tuple[int, dict[str, Any]]:
        """Issue one mutating request; return (status, body) for 2xx/handled."""
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "jn-engine-contributor-mcp",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        try:
            response = self._client.request(
                method, path, json=json_body, headers=headers
            )
        except httpx.RequestError as exc:
            raise upstream_unavailable() from exc
        self.statuses.append(response.status_code)
        if 200 <= response.status_code < 300 or response.status_code in handled:
            try:
                body = response.json()
            except ValueError as exc:
                raise upstream_unavailable() from exc
            if not isinstance(body, dict):
                raise upstream_unavailable()
            return response.status_code, body
        if response.status_code == 401 or response.status_code == 403:
            raise credential_unavailable()
        if response.status_code == 404:
            raise not_found()
        raise upstream_unavailable()


class GitHubWriteClient(GitHubReadClient):
    """Same construction and fail-closed credential handling; write sessions."""

    def begin(self) -> GitHubWriteSession:
        try:
            token = self._token_loader()
        except (OSError, ValueError) as exc:
            raise credential_unavailable() from exc
        if not token:
            raise credential_unavailable()
        return GitHubWriteSession(self._client, token, sleeper=self._sleeper)
