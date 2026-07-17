"""Isolated device OAuth, verifier, and headless minting acceptance tests."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from authlib.jose import JsonWebToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import app.mcp.device_oauth as device_module
from app.auth import AuthDecisionState, Principal
from app.config import Settings
from app.main import create_app
from app.mcp.device_oauth import (
    CLIENT_REGISTRY_FILE,
    JN_READ_SCOPE,
    load_signing_key,
)
from scripts import mint_device_token as mint_module
from tests.conftest import write_private

REAL_SNAPSHOT = Path(
    os.environ.get(
        "JN_TEST_SNAPSHOT_PATH",
        "/srv/jn-engine-contributor-mcp/test-snapshots/"
        "925242073a771aa68996c294aec8cc41cb43a0ef",
    )
)
BASE_URL = "http://localhost:8788"
REDIRECT_URI = "http://127.0.0.1:19191/callback"
ENROLLMENT_SECRET = "unit-test-device-enrollment-secret"
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def _private_rsa_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@pytest.fixture(autouse=True)
def _no_enrollment_delay(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(device_module, "ENROLL_FAILURE_DELAY_SECONDS", 0.0)


@pytest.fixture(scope="session")
def signing_key_pem() -> bytes:
    return _private_rsa_pem()


@pytest.fixture()
def device_settings(tmp_path: Path, signing_key_pem: bytes) -> Settings:
    secrets_dir = tmp_path / "device-secrets"
    secrets_dir.mkdir(mode=0o700)
    signing_key = write_private(
        secrets_dir / "oauth_jwt_signing_key",
        signing_key_pem,
    )
    enrollment_secret = write_private(
        secrets_dir / "mcp_enrollment_secret",
        ENROLLMENT_SECRET.encode("ascii"),
    )
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(mode=0o700, exist_ok=True)
    audit_dir.chmod(0o700)
    return Settings(
        app_env="development",
        api_host="127.0.0.1",
        public_base_url=BASE_URL,
        auth_mode="device",
        jn_snapshot_path=REAL_SNAPSHOT,
        search_engine="python",
        gateway_secrets_dir=secrets_dir,
        oauth_jwt_signing_key_file=signing_key,
        mcp_enrollment_secret_file=enrollment_secret,
        github_actions_read_token_file=secrets_dir / "github_actions_read_token",
        audit_log_path=audit_dir / "tool_calls.ndjson",
        oauth_allowed_client_redirect_uris=("http://127.0.0.1:*",),
    )


@pytest.fixture()
def device_client(device_settings: Settings):
    with TestClient(create_app(device_settings), follow_redirects=False) as client:
        yield client


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _register(client: TestClient, redirect_uri: str = REDIRECT_URI) -> dict:
    response = client.post(
        "/register",
        json={
            "redirect_uris": [redirect_uri],
            "client_name": "test-device-connector",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
            "scope": JN_READ_SCOPE,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _authorize_transaction(
    client: TestClient,
    client_id: str,
    challenge: str,
) -> str:
    response = client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "device-test-state",
            "scope": JN_READ_SCOPE,
        },
    )
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(f"{BASE_URL}/enroll?txn=")
    return parse_qs(urlparse(location).query)["txn"][0]


def _obtain_tokens(client: TestClient) -> dict:
    registration = _register(client)
    verifier, challenge = _pkce_pair()
    transaction = _authorize_transaction(
        client,
        registration["client_id"],
        challenge,
    )
    approved = client.post(
        "/enroll",
        data={"txn": transaction, "secret": ENROLLMENT_SECRET},
    )
    assert approved.status_code == 302, approved.text
    assert approved.headers["cache-control"] == "no-store"
    redirect = parse_qs(urlparse(approved.headers["location"]).query)
    assert redirect["state"] == ["device-test-state"]
    response = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": redirect["code"][0],
            "redirect_uri": REDIRECT_URI,
            "client_id": registration["client_id"],
            "client_secret": registration["client_secret"],
            "code_verifier": verifier,
        },
    )
    assert response.status_code == 200, response.text
    return {**response.json(), "registration": registration}


def _mcp_post(client: TestClient, token: str, payload: dict):
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}", **MCP_HEADERS},
        json=payload,
    )


def _rpc_result(response) -> dict:
    assert response.status_code == 200, response.text
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()["result"]
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line.removeprefix("data: "))["result"]
    raise AssertionError("MCP response did not contain a JSON-RPC result")


def _mint(
    settings: Settings,
    *,
    claims_update: dict[str, object] | None = None,
    remove: tuple[str, ...] = (),
    key_file: Path | None = None,
) -> str:
    key = load_signing_key(key_file or settings.oauth_jwt_signing_key_file)
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": BASE_URL,
        "aud": f"{BASE_URL}/mcp",
        "sub": "test-device",
        "client_id": "test-device",
        "scope": JN_READ_SCOPE,
        "iat": now,
        "exp": now + 300,
        "jti": secrets.token_urlsafe(8),
        "token_use": "access",
    }
    claims.update(claims_update or {})
    for name in remove:
        claims.pop(name, None)
    return (
        JsonWebToken(["RS256"])
        .encode(
            {"alg": "RS256", "typ": "JWT", "kid": key.kid},
            claims,
            key.private_pem,
        )
        .decode()
    )


def test_device_discovery_metadata_and_jwks(device_client: TestClient):
    metadata = device_client.get("/.well-known/oauth-authorization-server")
    assert metadata.status_code == 200
    document = metadata.json()
    assert document["issuer"].rstrip("/") == BASE_URL
    assert document["authorization_endpoint"] == f"{BASE_URL}/authorize"
    assert document["token_endpoint"] == f"{BASE_URL}/token"
    assert document["registration_endpoint"] == f"{BASE_URL}/register"
    assert document["jwks_uri"] == f"{BASE_URL}/.well-known/jwks.json"
    assert document["code_challenge_methods_supported"] == ["S256"]
    assert document["scopes_supported"] == [JN_READ_SCOPE]

    protected = device_client.get("/.well-known/oauth-protected-resource/mcp")
    assert protected.status_code == 200
    assert protected.json()["resource"] == f"{BASE_URL}/mcp"
    assert protected.json()["scopes_supported"] == [JN_READ_SCOPE]

    jwks = device_client.get("/.well-known/jwks.json").json()["keys"]
    assert len(jwks) == 1
    assert jwks[0]["kty"] == "RSA"
    assert jwks[0]["alg"] == "RS256"
    assert jwks[0]["use"] == "sig"
    assert "d" not in jwks[0]


def test_device_flow_authorizes_same_principal_for_mcp_and_rest(
    device_client: TestClient,
):
    tokens = _obtain_tokens(device_client)
    assert tokens["token_type"] == "Bearer"
    assert tokens["scope"] == JN_READ_SCOPE
    access_token = tokens["access_token"]

    initialize = _mcp_post(
        device_client,
        access_token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        },
    )
    assert initialize.status_code == 200
    listed = _rpc_result(
        _mcp_post(
            device_client,
            access_token,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
    )
    assert [tool["name"] for tool in listed["tools"]] == [
        "search",
        "fetch",
        "list_tasks",
        "project_context",
        "lookup_symbol",
        "check_status",
    ]
    symbol = _rpc_result(
        _mcp_post(
            device_client,
            access_token,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "lookup_symbol",
                    "arguments": {"address": "00437c40"},
                },
            },
        )
    )
    assert symbol["isError"] is False
    assert symbol["structuredContent"]["results"][0]["name"] == "UpdateGroundMoveA"

    rest = device_client.get(
        "/v1/re/symbols",
        params={"fourcc": "3AIT"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert rest.status_code == 200
    assert rest.json()["results"][0]["fourcc"] == "3AIT"

    provider = device_client.app.state.authenticator
    decision = asyncio.run(provider.authenticate_and_authorize(access_token))
    client_id = tokens["registration"]["client_id"]
    assert decision.state is AuthDecisionState.ALLOWED
    assert decision.principal == Principal(
        provider="device",
        subject=f"device:{client_id}",
        login=client_id,
        auth_mode="device",
    )


def test_refresh_grant_issues_a_new_device_access_token(device_client: TestClient):
    tokens = _obtain_tokens(device_client)
    registration = tokens["registration"]
    response = device_client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": registration["client_id"],
            "client_secret": registration["client_secret"],
        },
    )
    assert response.status_code == 200, response.text
    renewed = response.json()
    assert renewed["access_token"] != tokens["access_token"]
    assert _mcp_post(device_client, renewed["access_token"], {}).status_code != 401


@pytest.mark.parametrize(
    ("claims_update", "remove"),
    [
        ({"exp": int(time.time()) - 10}, ()),
        ({"aud": "https://other.example/mcp"}, ()),
        ({"iss": "https://other.example"}, ()),
        ({"scope": "other.read"}, ()),
        ({"aud": f"{BASE_URL}/token", "token_use": "refresh"}, ()),
        ({}, ("token_use",)),
        ({}, ("client_id",)),
        ({}, ("sub",)),
        ({"client_id": "bad client"}, ()),
        ({"sub": "bad\nsubject"}, ()),
    ],
    ids=[
        "expired",
        "wrong-audience",
        "wrong-issuer",
        "missing-scope",
        "refresh-as-access",
        "missing-token-use",
        "missing-client-id",
        "missing-subject",
        "unsafe-client-id",
        "unsafe-subject",
    ],
)
def test_invalid_device_token_classes_are_rejected(
    device_client: TestClient,
    device_settings: Settings,
    claims_update: dict[str, object],
    remove: tuple[str, ...],
):
    token = _mint(device_settings, claims_update=claims_update, remove=remove)
    response = _mcp_post(device_client, token, {})
    assert response.status_code == 401
    assert "resource_metadata=" in response.headers["www-authenticate"]


def test_garbage_and_foreign_signature_are_rejected(
    device_client: TestClient,
    device_settings: Settings,
    tmp_path: Path,
):
    assert _mcp_post(device_client, "not-a-jwt", {}).status_code == 401
    foreign_file = tmp_path / "foreign-key"
    write_private(foreign_file, _private_rsa_pem())
    foreign = _mint(device_settings, key_file=foreign_file)
    assert _mcp_post(device_client, foreign, {}).status_code == 401


def test_dcr_rejects_redirect_outside_explicit_allowlist(device_client: TestClient):
    response = device_client.post(
        "/register",
        json={
            "redirect_uris": ["https://attacker.example/callback"],
            "client_name": "untrusted-client",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"
    assert "attacker.example" not in response.text


def test_wrong_enrollment_secret_never_issues_a_code(device_client: TestClient):
    registration = _register(device_client)
    _, challenge = _pkce_pair()
    transaction = _authorize_transaction(
        device_client,
        registration["client_id"],
        challenge,
    )
    for _ in range(device_module.MAX_ENROLL_ATTEMPTS):
        response = device_client.post(
            "/enroll",
            data={"txn": transaction, "secret": "wrong-secret"},
        )
        assert response.status_code == 401
        assert "location" not in response.headers
    rejected = device_client.post(
        "/enroll",
        data={"txn": transaction, "secret": ENROLLMENT_SECRET},
    )
    assert rejected.status_code == 400


def test_forged_enrollment_transaction_is_rejected(device_client: TestClient):
    assert device_client.get("/enroll", params={"txn": "forged"}).status_code == 400
    response = device_client.post(
        "/enroll",
        data={"txn": "forged", "secret": ENROLLMENT_SECRET},
    )
    assert response.status_code == 400


def test_signing_key_client_registry_and_token_survive_restart(
    device_settings: Settings,
):
    with TestClient(create_app(device_settings), follow_redirects=False) as first:
        tokens = _obtain_tokens(first)
        kid_before = first.get("/.well-known/jwks.json").json()["keys"][0]["kid"]

    registry = device_settings.gateway_secrets_dir / CLIENT_REGISTRY_FILE
    assert registry.is_file()
    assert registry.stat().st_mode & 0o777 == 0o600

    with TestClient(create_app(device_settings), follow_redirects=False) as second:
        kid_after = second.get("/.well-known/jwks.json").json()["keys"][0]["kid"]
        assert _mcp_post(second, tokens["access_token"], {}).status_code != 401
        provider = second.app.state.authenticator
        registration = asyncio.run(
            provider.get_client(tokens["registration"]["client_id"])
        )
        assert registration is not None
    assert kid_after == kid_before


def test_device_client_registry_must_remain_private(device_settings: Settings):
    with TestClient(create_app(device_settings)) as client:
        _register(client)
    registry = device_settings.gateway_secrets_dir / CLIENT_REGISTRY_FILE
    registry.chmod(0o644)
    with pytest.raises(ValueError, match="mode 0600"):
        create_app(device_settings)


def test_device_mode_fails_on_non_rsa_signing_material(device_settings: Settings):
    device_settings.oauth_jwt_signing_key_file.write_bytes(secrets.token_bytes(48))
    device_settings.oauth_jwt_signing_key_file.chmod(0o600)
    with pytest.raises(ValueError, match="PKCS8 RSA private key"):
        create_app(device_settings)


def test_device_token_is_rejected_when_github_mode_is_active(
    github_settings: Settings,
    device_settings: Settings,
):
    token = _mint(device_settings)
    github = github_settings.model_copy(update={"jn_snapshot_path": REAL_SNAPSHOT})
    with TestClient(create_app(github)) as client:
        response = _mcp_post(client, token, {})
        assert response.status_code == 401
        assert client.get("/enroll").status_code == 404
        assert client.get("/.well-known/jwks.json").status_code == 404


def test_device_production_requires_redirect_allowlist(device_settings: Settings):
    with pytest.raises(ValueError, match="must not be empty"):
        Settings(
            **device_settings.model_copy(
                update={
                    "app_env": "production",
                    "public_base_url": "https://jn-ai.example",
                    "oauth_allowed_client_redirect_uris": (),
                }
            ).model_dump()
        )


def test_headless_mint_emits_named_jn_read_access_token(
    tmp_path: Path,
    signing_key_pem: bytes,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(mode=0o700)
    key_file = write_private(
        secrets_dir / "oauth_jwt_signing_key",
        signing_key_pem,
    )
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PUBLIC_BASE_URL=https://jn-ai.example\n"
        "MCP_PATH=/mcp\n"
        f"GATEWAY_SECRETS_HOST_PATH={secrets_dir}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mint_device_token.py",
            "--client-id",
            "contributor-device",
            "--ttl-days",
            "1",
            "--env-file",
            str(env_file),
        ],
    )
    assert mint_module.main() == 0
    captured = capsys.readouterr()
    token = captured.out.strip()
    assert token
    assert token not in captured.err
    key = load_signing_key(key_file)
    claims = JsonWebToken(["RS256"]).decode(token, key.public_pem)
    assert claims["iss"] == "https://jn-ai.example"
    assert claims["aud"] == "https://jn-ai.example/mcp"
    assert claims["sub"] == "contributor-device"
    assert claims["client_id"] == "contributor-device"
    assert claims["scope"] == JN_READ_SCOPE
    assert claims["token_use"] == "access"
