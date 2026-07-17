"""Shared filesystem and settings fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Settings
from app.core.snapshot import (
    APPROVED_ROOT_DIRECTORIES,
    INCLUDED_ROOTS,
    compute_content_inventory,
)

GROUNDING_COMMIT = "925242073a771aa68996c294aec8cc41cb43a0ef"


@pytest.fixture()
def snapshot(tmp_path: Path) -> Path:
    root = tmp_path / "snapshot"
    content = root / "content"
    content.mkdir(parents=True)
    for directory in APPROVED_ROOT_DIRECTORIES:
        (content / directory).mkdir()
    for name in ("AGENTS.md", "README.md", "Makefile"):
        (content / name).write_text(f"{name}\n", encoding="utf-8")
    inventory = compute_content_inventory(content)
    manifest = {
        "schema_version": 1,
        "repository": "alexscott2718-gif/jn-engine",
        "ref": "refs/heads/master",
        "commit": GROUNDING_COMMIT,
        "tree": "1" * 40,
        "commit_time": "2026-07-11T00:00:00Z",
        "created_at": "2026-07-11T00:01:00Z",
        "file_count": inventory.file_count,
        "total_bytes": inventory.total_bytes,
        "content_sha256": inventory.content_sha256,
        "included_roots": list(INCLUDED_ROOTS),
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def write_private(path: Path, value: bytes) -> Path:
    path.write_bytes(value)
    path.chmod(0o600)
    return path


def make_github_settings(
    tmp_path: Path,
    snapshot: Path,
    **overrides,
) -> Settings:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(mode=0o700, exist_ok=True)
    secrets_dir.chmod(0o700)
    oauth_secret = write_private(
        secrets_dir / "github_oauth_client_secret",
        b"unit-test-oauth-client-secret",
    )
    collaborator_token = write_private(
        secrets_dir / "github_collaborator_token",
        b"unit-test-collaborator-token-not-real-123",
    )
    actions_read_token = write_private(
        secrets_dir / "github_actions_read_token",
        b"unit-test-read-only-actions-token-123",
    )
    jwt_key = write_private(
        secrets_dir / "oauth_jwt_signing_key",
        b"unit-test-jwt-signing-material-32-bytes-minimum",
    )
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(mode=0o700, exist_ok=True)
    audit_dir.chmod(0o700)
    values = {
        "app_env": "development",
        "api_host": "127.0.0.1",
        "public_base_url": "http://localhost:8788",
        "auth_mode": "github",
        "jn_snapshot_path": snapshot,
        "gateway_secrets_dir": secrets_dir,
        "github_oauth_client_id": "Ov23unit-test-client",
        "github_oauth_client_secret_file": oauth_secret,
        "github_collaborator_token_file": collaborator_token,
        "github_actions_read_token_file": actions_read_token,
        "audit_log_path": audit_dir / "tool_calls.ndjson",
        "task_claim_ledger_path": audit_dir / "task_claims.ndjson",
        "oauth_jwt_signing_key_file": jwt_key,
        "oauth_allowed_client_redirect_uris": ("http://127.0.0.1:*",),
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture()
def github_settings(tmp_path: Path, snapshot: Path) -> Settings:
    return make_github_settings(tmp_path, snapshot)
