"""Exact six-route REST surface, shared auth decisions, and error contract."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.auth import AuthDecision, AuthDecisionState, Principal
from app.core.path_safety import encode_content_id
from app.main import create_app
from app.mcp.github_oauth import ContributorGitHubProvider
from uvicorn import Config, Server

REAL_SNAPSHOT = Path(
    os.environ.get(
        "JN_TEST_SNAPSHOT_PATH",
        "/srv/jn-engine-contributor-mcp/test-snapshots/"
        "925242073a771aa68996c294aec8cc41cb43a0ef",
    )
)
COMMIT = "925242073a771aa68996c294aec8cc41cb43a0ef"
PROTECTED_ROUTES = (
    "/v1/content/search",
    "/v1/content/fetch",
    "/v1/tasks/list",
    "/v1/projects/context",
    "/v1/re/symbols",
)
ALL_DATA_ROUTES = ("/health", *PROTECTED_ROUTES)


def _allowed_decision() -> AuthDecision:
    return AuthDecision(
        AuthDecisionState.ALLOWED,
        principal=Principal(
            provider="github",
            subject="github:42",
            login="alice",
            auth_mode="github",
        ),
    )


@pytest.fixture()
def rest_gateway(github_settings, monkeypatch: pytest.MonkeyPatch):
    state = {
        "decision": _allowed_decision(),
        "calls": [],
    }

    async def authenticate(_self, token: str):
        state["calls"].append(token)
        return state["decision"]

    monkeypatch.setattr(
        ContributorGitHubProvider,
        "authenticate_and_authorize",
        authenticate,
    )
    settings = github_settings.model_copy(
        update={
            "jn_snapshot_path": REAL_SNAPSHOT,
            "search_engine": "python",
        }
    )
    application = create_app(settings)
    with TestClient(application) as client:
        yield client, application, state


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer gateway-token"}


def _route_request(path: str):
    if path == "/v1/content/search":
        return {"q": "C3DPlayer", "scope": "re", "limit": 1}
    if path == "/v1/content/fetch":
        return {"id": encode_content_id(COMMIT, "README.md")}
    if path == "/v1/tasks/list":
        return {"status": "open", "limit": 1}
    if path == "/v1/projects/context":
        return {"max_chars": 1_000}
    if path == "/v1/re/symbols":
        return {"address": "00437c40"}
    raise AssertionError(path)


def _assert_error(response, status: int, code: str) -> None:
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"code", "detail", "request_id"}
    assert body["code"] == code
    assert len(body["request_id"]) == 32
    assert response.headers["x-request-id"] == body["request_id"]


def test_exact_six_get_routes_and_no_schema_routes(rest_gateway):
    client, application, _state = rest_gateway
    api_routes: list[APIRoute] = []
    for route in application.routes:
        if isinstance(route, APIRoute):
            api_routes.append(route)
        elif hasattr(route, "original_router"):
            api_routes.extend(
                candidate
                for candidate in route.original_router.routes
                if isinstance(candidate, APIRoute)
            )
    routes = {route.path: route.methods for route in api_routes}
    assert tuple(routes) == ALL_DATA_ROUTES
    assert all(methods == {"GET"} for methods in routes.values())
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.post("/v1/content/search").status_code in {404, 405}


def test_health_is_public_and_never_calls_auth(rest_gateway):
    client, _application, state = rest_gateway
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "app": "jn-engine-contributor-mcp",
        "mode": "read_only",
        "commit": COMMIT,
    }
    assert state["calls"] == []


def test_untrusted_host_is_rejected_before_data_access(rest_gateway):
    client, _application, state = rest_gateway
    response = client.get("/health", headers={"Host": "attacker.example"})
    assert response.status_code == 400
    assert state["calls"] == []


def test_every_protected_route_returns_shared_401_without_bearer(rest_gateway):
    client, _application, state = rest_gateway
    for path in PROTECTED_ROUTES:
        response = client.get(path, params=_route_request(path))
        _assert_error(response, 401, "unauthenticated")
        assert response.headers["www-authenticate"] == "Bearer"
    assert state["calls"] == []


@pytest.mark.parametrize(
    ("decision", "status", "code"),
    [
        (AuthDecision(AuthDecisionState.UNAUTHENTICATED), 401, "unauthenticated"),
        (AuthDecision(AuthDecisionState.FORBIDDEN), 403, "forbidden"),
        (
            AuthDecision(AuthDecisionState.AUTH_DEPENDENCY_UNAVAILABLE),
            503,
            "auth_dependency_unavailable",
        ),
    ],
)
def test_shared_principal_preserves_401_403_503(
    rest_gateway,
    decision: AuthDecision,
    status: int,
    code: str,
):
    client, _application, state = rest_gateway
    state["decision"] = decision
    response = client.get(
        "/v1/re/symbols",
        params={"name": "C3DPlayer"},
        headers=_headers(),
    )
    _assert_error(response, status, code)
    assert state["calls"] == ["gateway-token"]


def test_authorized_contributor_calls_all_five_real_routes(rest_gateway):
    client, _application, state = rest_gateway
    bodies: dict[str, dict] = {}
    for path in PROTECTED_ROUTES:
        response = client.get(
            path,
            params=_route_request(path),
            headers=_headers(),
        )
        assert response.status_code == 200, (path, response.text)
        bodies[path] = response.json()

    search = bodies["/v1/content/search"]
    assert search["snapshot"]["commit"] == COMMIT
    assert search["results"][0]["path"] == "docs/decomp/C3DPlayer.md"
    assert "content" not in search["results"][0]
    assert search["results"][0]["url"].startswith("https://github.com/")

    fetched = bodies["/v1/content/fetch"]
    assert fetched["metadata"]["path"] == "README.md"
    assert fetched["metadata"]["commit"] == COMMIT
    assert fetched["metadata"]["text_chars"] == len(fetched["text"])

    tasks = bodies["/v1/tasks/list"]
    assert tasks["count"] == 1
    assert tasks["tasks"][0]["source_kind"] != "issues"

    context = bodies["/v1/projects/context"]
    assert len(context["context"]) <= 1_000
    assert len(context["important_files"]) == 8

    symbols = bodies["/v1/re/symbols"]
    assert symbols["results"][0]["name"] == "UpdateGroundMoveA"
    assert symbols["results"][0]["address"] == "00437c40"
    assert len(state["calls"]) == 5

    serialized = json.dumps(bodies)
    for forbidden in (
        "/home/",
        "/data/",
        "/secrets/",
        "gateway-token",
        "github_pat_unit_test",
    ):
        assert forbidden not in serialized


def test_search_content_is_present_only_when_requested(rest_gateway):
    client, _application, _state = rest_gateway
    response = client.get(
        "/v1/content/search",
        params={
            "q": "C3DPlayer",
            "scope": "re",
            "limit": 1,
            "include_content": "true",
        },
        headers=_headers(),
    )
    assert response.status_code == 200
    assert 0 < len(response.json()["results"][0]["content"]) <= 10_000


@pytest.mark.parametrize(
    ("content_id", "status", "code"),
    [
        ("../../etc/passwd", 400, "invalid_id"),
        (encode_content_id("1" * 40, "README.md"), 409, "snapshot_changed"),
        (encode_content_id(COMMIT, "docs/not-present.md"), 404, "not_found"),
    ],
)
def test_fetch_error_matrix_is_sanitized(
    rest_gateway,
    content_id: str,
    status: int,
    code: str,
):
    client, _application, _state = rest_gateway
    response = client.get(
        "/v1/content/fetch",
        params={"id": content_id},
        headers=_headers(),
    )
    _assert_error(response, status, code)
    assert "/home/" not in response.text
    assert "docs/not-present.md" not in response.text


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/v1/content/search", {"q": ""}),
        ("/v1/tasks/list", {"limit": 101}),
        ("/v1/projects/context", {"max_chars": 999}),
        ("/v1/re/symbols", {}),
        ("/v1/re/symbols", {"fourcc": "ABC"}),
    ],
)
def test_request_validation_is_400_never_default_422(
    rest_gateway,
    path: str,
    params: dict[str, object],
):
    client, _application, _state = rest_gateway
    response = client.get(path, params=params, headers=_headers())
    _assert_error(response, 400, "invalid_request")
    assert "422" not in response.text
    assert "input" not in response.json()


def test_live_http_rest_auth_matrix_and_all_five_routes(
    github_settings,
    monkeypatch: pytest.MonkeyPatch,
):
    state = {"decision": _allowed_decision()}

    async def authenticate(_self, _token: str):
        return state["decision"]

    monkeypatch.setattr(
        ContributorGitHubProvider,
        "authenticate_and_authorize",
        authenticate,
    )
    settings = github_settings.model_copy(
        update={
            "jn_snapshot_path": REAL_SNAPSHOT,
            "search_engine": "python",
        }
    )
    application = create_app(settings)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = Server(
        Config(
            application,
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
            assert client.get("/health").json()["commit"] == COMMIT
            for path in PROTECTED_ROUTES:
                response = client.get(path, params=_route_request(path))
                _assert_error(response, 401, "unauthenticated")

            state["decision"] = AuthDecision(AuthDecisionState.FORBIDDEN)
            forbidden = client.get(
                "/v1/re/symbols",
                params={"name": "C3DPlayer"},
                headers=_headers(),
            )
            _assert_error(forbidden, 403, "forbidden")

            state["decision"] = AuthDecision(
                AuthDecisionState.AUTH_DEPENDENCY_UNAVAILABLE
            )
            unavailable = client.get(
                "/v1/re/symbols",
                params={"name": "C3DPlayer"},
                headers=_headers(),
            )
            _assert_error(
                unavailable,
                503,
                "auth_dependency_unavailable",
            )

            state["decision"] = _allowed_decision()
            for path in PROTECTED_ROUTES:
                response = client.get(
                    path,
                    params=_route_request(path),
                    headers=_headers(),
                )
                assert response.status_code == 200, (path, response.text)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()
