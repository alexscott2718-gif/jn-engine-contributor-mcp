"""GitHub identity, collaborator, cache, and surface-status behavior."""

from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.github import GitHubProvider

from app.auth import (
    AuthDecision,
    AuthDecisionState,
    Principal,
    build_rest_principal_dependency,
)
from app.config import GITHUB_API_VERSION
from app.mcp.github_oauth import (
    CollaboratorDecision,
    CollaboratorState,
    ContributorAuthorizer,
    ContributorGitHubProvider,
    create_github_provider,
)
from app.main import create_app
from scripts import check_github_collaborator_token as preflight_module


def test_allow_is_cached_with_fixed_request_contract():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == (
            "/repos/alexscott2718-gif/jn-engine/collaborators/Alice"
        )
        assert request.headers["x-github-api-version"] == GITHUB_API_VERSION
        assert request.headers["authorization"] == "Bearer server-token-value"
        return httpx.Response(204, request=request)

    async def scenario():
        async with httpx.AsyncClient(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            authorizer = ContributorAuthorizer(
                server_token="server-token-value",
                client=client,
            )
            first = await authorizer.check(user_id="42", login="Alice")
            second = await authorizer.check(user_id="42", login="alice")
            return first, second

    first, second = asyncio.run(scenario())
    assert first.state is CollaboratorState.ALLOWED
    assert first.status_code == 204
    assert second.reason == "cache"
    assert calls == 1


def test_negative_cache_expires_at_its_shorter_ttl():
    calls = 0
    now = [0.0]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, request=request)

    async def scenario():
        async with httpx.AsyncClient(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            authorizer = ContributorAuthorizer(
                server_token="server-token-value",
                positive_ttl_seconds=300,
                negative_ttl_seconds=60,
                client=client,
                clock=lambda: now[0],
            )
            first = await authorizer.check(user_id="42", login="alice")
            now[0] = 59
            cached = await authorizer.check(user_id="42", login="alice")
            now[0] = 60
            expired = await authorizer.check(user_id="42", login="alice")
            return first, cached, expired

    first, cached, expired = asyncio.run(scenario())
    assert first.state is CollaboratorState.DENIED
    assert cached.reason == "cache"
    assert expired.reason == "github"
    assert calls == 2


def test_positive_cache_expiry_observes_collaborator_removal():
    calls = 0
    now = [0.0]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        status_code = 204 if calls == 1 else 404
        return httpx.Response(status_code, request=request)

    async def scenario():
        async with httpx.AsyncClient(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            authorizer = ContributorAuthorizer(
                server_token="server-token-value",
                positive_ttl_seconds=300,
                negative_ttl_seconds=60,
                client=client,
                clock=lambda: now[0],
            )
            allowed = await authorizer.check(user_id="42", login="alice")
            now[0] = 299
            cached = await authorizer.check(user_id="42", login="alice")
            now[0] = 300
            removed = await authorizer.check(user_id="42", login="alice")
            return allowed, cached, removed

    allowed, cached, removed = asyncio.run(scenario())
    assert allowed.state is CollaboratorState.ALLOWED
    assert cached.state is CollaboratorState.ALLOWED
    assert cached.reason == "cache"
    assert removed.state is CollaboratorState.DENIED
    assert calls == 2


@pytest.mark.parametrize("status_code", [401, 403, 429, 500, 503])
def test_upstream_errors_fail_closed_and_are_not_cached(status_code: int):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code, request=request)

    async def scenario():
        async with httpx.AsyncClient(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            authorizer = ContributorAuthorizer(
                server_token="server-token-value",
                client=client,
            )
            first = await authorizer.check(user_id="42", login="alice")
            second = await authorizer.check(user_id="42", login="alice")
            return first, second

    first, second = asyncio.run(scenario())
    assert first.state is CollaboratorState.UNAVAILABLE
    assert second.state is CollaboratorState.UNAVAILABLE
    assert calls == 2


def test_network_error_fails_closed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("test timeout", request=request)

    async def scenario():
        async with httpx.AsyncClient(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            authorizer = ContributorAuthorizer(
                server_token="server-token-value",
                client=client,
            )
            return await authorizer.check(user_id="42", login="alice")

    decision = asyncio.run(scenario())
    assert decision.state is CollaboratorState.UNAVAILABLE
    assert decision.status_code is None


def _identity() -> AccessToken:
    return AccessToken(
        token="upstream-token-must-not-appear-in-repr",
        client_id="42",
        scopes=["read:user"],
        claims={"sub": "42", "login": "alice"},
    )


class _StubAuthorizer:
    def __init__(self, decision: CollaboratorDecision) -> None:
        self.decision = decision

    async def check(self, *, user_id: str, login: str) -> CollaboratorDecision:
        assert (user_id, login) == ("42", "alice")
        return self.decision


@pytest.mark.parametrize(
    ("collaborator_state", "auth_state"),
    [
        (CollaboratorState.ALLOWED, AuthDecisionState.ALLOWED),
        (CollaboratorState.DENIED, AuthDecisionState.FORBIDDEN),
        (
            CollaboratorState.UNAVAILABLE,
            AuthDecisionState.AUTH_DEPENDENCY_UNAVAILABLE,
        ),
    ],
)
def test_provider_maps_verified_identity_to_typed_decision(
    monkeypatch: pytest.MonkeyPatch,
    collaborator_state: CollaboratorState,
    auth_state: AuthDecisionState,
):
    async def verified(_self, _token: str):
        return _identity()

    monkeypatch.setattr(GitHubProvider, "verify_token", verified)
    provider = object.__new__(ContributorGitHubProvider)
    provider._contributor_authorizer = _StubAuthorizer(
        CollaboratorDecision(collaborator_state, 204, "test")
    )

    decision = asyncio.run(provider.authenticate_and_authorize("gateway-token"))
    assert decision.state is auth_state
    if auth_state is AuthDecisionState.ALLOWED:
        assert decision.principal == Principal(
            provider="github",
            subject="github:42",
            login="alice",
            auth_mode="github",
        )
        assert "upstream-token" not in repr(decision)


def test_provider_constructor_uses_read_user_and_encrypted_filetree_state(
    github_settings,
):
    provider, authorizer = create_github_provider(github_settings)
    try:
        assert provider.required_scopes == ["read:user"]
        state_dir = github_settings.gateway_secrets_dir / "oauth-proxy"
        assert state_dir.is_dir()
        assert state_dir.stat().st_mode & 0o777 == 0o700
        for state_file in state_dir.rglob("*"):
            if state_file.is_file():
                assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    finally:
        asyncio.run(authorizer.aclose())


def test_github_mode_mounts_discovery_and_challenges_mcp(github_settings):
    real_snapshot = Path(
        os.environ.get(
            "JN_TEST_SNAPSHOT_PATH",
            "/srv/jn-engine-contributor-mcp/test-snapshots/"
            "925242073a771aa68996c294aec8cc41cb43a0ef",
        )
    )
    settings = github_settings.model_copy(
        update={"jn_snapshot_path": real_snapshot}
    )
    with TestClient(create_app(settings)) as client:
        metadata = client.get("/.well-known/oauth-authorization-server")
        assert metadata.status_code == 200
        document = metadata.json()
        assert document["issuer"].rstrip("/") == "http://localhost:8788"
        assert document["authorization_endpoint"] == "http://localhost:8788/authorize"
        assert document["token_endpoint"] == "http://localhost:8788/token"
        assert document["registration_endpoint"] == "http://localhost:8788/register"

        protected = client.get(
            "/.well-known/oauth-protected-resource/mcp"
        )
        assert protected.status_code == 200
        assert protected.json()["resource"] == "http://localhost:8788/mcp"

        challenged = client.post(
            "/mcp",
            json={},
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert challenged.status_code == 401
        assert challenged.headers["www-authenticate"].startswith("Bearer ")
        assert "resource_metadata=" in challenged.headers["www-authenticate"]


class _DecisionAuthenticator:
    def __init__(self, decision: AuthDecision) -> None:
        self.decision = decision

    async def authenticate_and_authorize(self, token: str) -> AuthDecision:
        assert token == "gateway-token"
        return self.decision


def _rest_status(decision: AuthDecision, *, token: bool = True) -> int:
    app = FastAPI()
    dependency = build_rest_principal_dependency(_DecisionAuthenticator(decision))

    @app.get("/protected")
    async def protected(_principal: Principal = Depends(dependency)):
        return {"ok": True}

    headers = {"Authorization": "Bearer gateway-token"} if token else {}
    return TestClient(app).get("/protected", headers=headers).status_code


def test_rest_preserves_401_403_503_split():
    assert _rest_status(AuthDecision(AuthDecisionState.UNAUTHENTICATED)) == 401
    assert _rest_status(AuthDecision(AuthDecisionState.FORBIDDEN)) == 403
    assert (
        _rest_status(AuthDecision(AuthDecisionState.AUTH_DEPENDENCY_UNAVAILABLE))
        == 503
    )
    assert _rest_status(AuthDecision(AuthDecisionState.UNAUTHENTICATED), token=False) == 401


@pytest.mark.parametrize(
    ("decision", "expected_exit"),
    [
        (
            CollaboratorDecision(CollaboratorState.ALLOWED, 204, "github"),
            0,
        ),
        (
            CollaboratorDecision(CollaboratorState.UNAVAILABLE, 403, "upstream_status"),
            2,
        ),
    ],
)
def test_operator_preflight_requires_204_and_never_prints_token(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    decision: CollaboratorDecision,
    expected_exit: int,
):
    token_value = "github_pat_unit_test_operator_token_12345"
    token_file = tmp_path / "token"
    token_file.write_text(token_value, encoding="utf-8")
    token_file.chmod(0o600)

    class FakeAuthorizer:
        def __init__(self, *, server_token: str) -> None:
            assert server_token == token_value

        async def preflight(self, *, login: str) -> CollaboratorDecision:
            assert login == "alice"
            return decision

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(preflight_module, "ContributorAuthorizer", FakeAuthorizer)
    exit_code = asyncio.run(
        preflight_module.run_preflight(login="alice", token_file=token_file)
    )
    output = capsys.readouterr().out
    assert exit_code == expected_exit
    assert token_value not in output
    assert f"status={decision.status_code}" in output
