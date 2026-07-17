"""Dedicated self-repository snapshot, tools, and dual-endpoint contracts."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from fastmcp.exceptions import ToolError

from app.config import Settings
from app.gateway_repo.content import GatewayRepositoryContent, encode_gateway_id
from app.gateway_repo.server import create_gateway_repository_mcp_server
from app.gateway_repo.snapshot import (
    APPROVED_ROOT_DIRECTORIES,
    APPROVED_ROOT_FILES,
    INCLUDED_ROOTS,
    GatewaySnapshotError,
    compute_gateway_inventory,
    validate_gateway_snapshot,
)
from app.gateway_repo_main import (
    build_gateway_repository_data_plane,
    create_gateway_repository_app,
)
from app.run import main as run_main
from deploy.export_gateway_snapshot import build_gateway_manifest

GATEWAY_COMMIT = "4" * 40


def _write_gateway_corpus(content: Path) -> None:
    content.mkdir(parents=True)
    for directory in APPROVED_ROOT_DIRECTORIES:
        (content / directory).mkdir(parents=True)
    root_values = {
        ".dockerignore": "*\n!app/**\n",
        ".env.example": "AUTH_MODE=github\n",
        ".env.gateway-repo.example": "SERVICE_PROFILE=gateway_repository\n",
        ".gitignore": ".env\n",
        "CONTRIBUTING.md": "# Contributing\n\nRun tests.\n",
        "Dockerfile": "FROM python:3.11-slim\nUSER 10001:10001\n",
        "LICENSE": "MIT License\n",
        "README.md": "# Gateway\n\nStreamable HTTP MCP service.\n",
        "SECURITY.md": "# Security\n\nReport privately.\n",
        "docker-compose.yml": "services:\n  gateway:\n    read_only: true\n",
        "docker-compose.gateway-repo.yml": (
            "services:\n  gateway-repo:\n    read_only: true\n"
        ),
        "pyproject.toml": "[project]\nname = 'jn-engine-contributor-mcp'\n",
        "requirements.lock": "fastmcp==3.4.4\n",
    }
    assert set(root_values) == APPROVED_ROOT_FILES
    for path, text in root_values.items():
        (content / path).write_text(text, encoding="utf-8")
    (content / ".github/workflows").mkdir()
    (content / ".github/workflows/tests.yml").write_text(
        "name: tests\n", encoding="utf-8"
    )
    (content / "app/main.py").write_text(
        "def create_app():\n    return 'app'\n", encoding="utf-8"
    )
    (content / "cloudflared/config.example.yml").write_text(
        "ingress: []\n", encoding="utf-8"
    )
    (content / "deploy/refresh_snapshot.sh").write_text(
        "#!/bin/sh\nset -eu\n", encoding="utf-8"
    )
    (content / "docs/intended_usage.md").write_text(
        "# Intended Usage\n\nImmutable snapshots.\n", encoding="utf-8"
    )
    (content / "docs/mcp_surface.md").write_text(
        "# MCP Surface\n\nThree tools.\n", encoding="utf-8"
    )
    (content / "docs/security_model.md").write_text(
        "# Security Model\n\nFail closed.\n", encoding="utf-8"
    )
    (content / "docs/deployment.md").write_text(
        "# Deployment\n\nUse immutable mounts.\n", encoding="utf-8"
    )
    (content / "docs/public_repository_boundary.md").write_text(
        "# Public Repository Boundary\n\nNo secrets.\n", encoding="utf-8"
    )
    (content / "scripts/validate_snapshot.py").write_text(
        "print('valid')\n", encoding="utf-8"
    )
    (content / "tests/test_mcp_tools.py").write_text(
        "def test_tools():\n    assert True\n", encoding="utf-8"
    )


@pytest.fixture()
def gateway_snapshot(tmp_path: Path) -> Path:
    root = tmp_path / "gateway-snapshot"
    _write_gateway_corpus(root / "content")
    inventory = compute_gateway_inventory(root / "content")
    manifest = {
        "schema_version": 1,
        "repository": "alexscott2718-gif/jn-engine-contributor-mcp",
        "ref": "refs/heads/main",
        "commit": GATEWAY_COMMIT,
        "tree": "5" * 40,
        "commit_time": "2026-07-16T00:00:00Z",
        "created_at": "2026-07-16T00:01:00Z",
        "file_count": inventory.file_count,
        "total_bytes": inventory.total_bytes,
        "content_sha256": inventory.content_sha256,
        "included_roots": list(INCLUDED_ROOTS),
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _make_read_only(root: Path) -> None:
    for current, directories, files in os.walk(root):
        for filename in files:
            (Path(current) / filename).chmod(0o444)
        for directory in directories:
            (Path(current) / directory).chmod(0o555)
    root.chmod(0o555)


def _tools(server):
    return {tool.name: tool for tool in asyncio.run(server.list_tools())}


def _run(tool, arguments):
    return asyncio.run(tool.run(arguments))


def test_corpus_includes_docker_compose_workflow_tests_and_deploy(gateway_snapshot: Path):
    snapshot = validate_gateway_snapshot(gateway_snapshot, require_read_only=False)
    paths = set(snapshot.files_by_path)
    assert {
        "Dockerfile",
        "docker-compose.yml",
        "docker-compose.gateway-repo.yml",
        ".github/workflows/tests.yml",
        "tests/test_mcp_tools.py",
        "deploy/refresh_snapshot.sh",
    } <= paths


@pytest.mark.parametrize("unsafe", [".env", "secrets/token.txt", ".github/private.txt"])
def test_corpus_rejects_unapproved_or_secret_shaped_paths(
    gateway_snapshot: Path,
    unsafe: str,
):
    path = gateway_snapshot / "content" / unsafe
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("must not ship\n", encoding="utf-8")
    with pytest.raises(GatewaySnapshotError, match="outside policy|unapproved"):
        validate_gateway_snapshot(gateway_snapshot, require_read_only=False)


def test_exporter_filters_unapproved_files_and_writes_strict_manifest(
    gateway_snapshot: Path,
):
    (gateway_snapshot / "manifest.json").unlink()
    (gateway_snapshot / "content/.env").write_text("SECRET=no\n", encoding="utf-8")
    (gateway_snapshot / "content/docs/image.png").write_bytes(b"png")
    manifest = build_gateway_manifest(
        gateway_snapshot,
        commit=GATEWAY_COMMIT,
        tree="5" * 40,
        commit_time="2026-07-16T00:00:00Z",
    )
    assert manifest.repository == "alexscott2718-gif/jn-engine-contributor-mcp"
    assert not (gateway_snapshot / "content/.env").exists()
    assert not (gateway_snapshot / "content/docs/image.png").exists()


def test_three_tools_search_fetch_context_and_id_separation(gateway_snapshot: Path):
    snapshot = validate_gateway_snapshot(gateway_snapshot, require_read_only=False)
    content = GatewayRepositoryContent(snapshot)
    server = create_gateway_repository_mcp_server(auth=None, content=content)
    tools = _tools(server)
    assert list(tools) == ["search", "fetch", "repository_context"]
    assert asyncio.run(server.list_resources()) == []
    assert asyncio.run(server.list_prompts()) == []
    for tool in tools.values():
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.idempotentHint is True
        assert tool.annotations.openWorldHint is False

    searched = _run(tools["search"], {"query": "python:3.11", "scope": "deploy"})
    hit = searched.structured_content["results"][0]
    assert hit["id"].startswith("jng1_")
    assert hit["title"] == "Dockerfile"
    fetched = _run(tools["fetch"], {"id": hit["id"]})
    assert fetched.structured_content["metadata"]["path"] == "Dockerfile"
    assert fetched.structured_content["metadata"]["kind"] == "deployment"
    assert fetched.structured_content["metadata"]["repository"].endswith(
        "/jn-engine-contributor-mcp"
    )
    compose_search = _run(
        tools["search"],
        {"query": "gateway-repo:", "scope": "deploy"},
    )
    assert compose_search.structured_content["results"][0]["title"] == (
        "docker-compose.gateway-repo.yml"
    )

    context = _run(tools["repository_context"], {"max_chars": 1_000})
    assert context.structured_content["commit"] == GATEWAY_COMMIT
    assert len(context.structured_content["context"]) <= 1_000
    assert "## docs/deployment.md" in context.structured_content["context"]
    with pytest.raises(ToolError, match="invalid gateway content ID"):
        _run(tools["fetch"], {"id": "jn1_not-a-gateway-id"})


def test_stale_gateway_id_requires_new_search(gateway_snapshot: Path):
    snapshot = validate_gateway_snapshot(gateway_snapshot, require_read_only=False)
    server = create_gateway_repository_mcp_server(
        auth=None,
        content=GatewayRepositoryContent(snapshot),
    )
    stale = encode_gateway_id("6" * 40, "README.md")
    with pytest.raises(ToolError, match="gateway snapshot changed; search again"):
        _run(_tools(server)["fetch"], {"id": stale})


def test_gateway_profile_builds_read_only_gateway_data_plane(
    gateway_snapshot: Path,
):
    _make_read_only(gateway_snapshot)
    settings = Settings(
        auth_mode="authless_local",
        api_host="127.0.0.1",
        service_profile="gateway_repository",
        gateway_repo_snapshot_path=gateway_snapshot,
        search_engine="python",
    )
    data = build_gateway_repository_data_plane(settings)
    assert data.snapshot.manifest.commit == GATEWAY_COMMIT


def test_gateway_profile_does_not_require_unused_actions_or_audit_credentials(
    github_settings: Settings,
    gateway_snapshot: Path,
):
    _make_read_only(gateway_snapshot)
    github_settings.github_actions_read_token_file.unlink()
    shutil.rmtree(github_settings.audit_log_path.parent)
    values = github_settings.model_dump()
    values.update(
        service_profile="gateway_repository",
        gateway_repo_snapshot_path=gateway_snapshot,
    )
    settings = Settings(**values)
    assert settings.service_profile == "gateway_repository"


def test_process_entrypoint_selects_dedicated_gateway_app(
    gateway_snapshot: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    settings = Settings(
        auth_mode="authless_local",
        api_host="127.0.0.1",
        service_profile="gateway_repository",
        gateway_repo_snapshot_path=gateway_snapshot,
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr("app.run.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.run.uvicorn.run",
        lambda target, **kwargs: captured.update(target=target, **kwargs),
    )
    run_main()
    assert captured["target"] == "app.gateway_repo_main:app"


def test_dedicated_app_has_its_own_origin_bound_oauth_resource(
    github_settings: Settings,
    gateway_snapshot: Path,
):
    _make_read_only(gateway_snapshot)
    settings = github_settings.model_copy(
        update={
            "service_profile": "gateway_repository",
            "public_base_url": "https://jn-gateway-ai.example",
            "gateway_repo_snapshot_path": gateway_snapshot,
        }
    )
    with TestClient(create_gateway_repository_app(settings)) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["commit"] == GATEWAY_COMMIT
        gateway_metadata = client.get("/.well-known/oauth-protected-resource/mcp")
        assert gateway_metadata.status_code == 200
        assert gateway_metadata.json()["resource"] == (
            "https://jn-gateway-ai.example/mcp"
        )
        headers = {"Accept": "application/json, text/event-stream"}
        response = client.post("/mcp", json={}, headers=headers)
        assert response.status_code == 401
        assert "resource_metadata=" in response.headers["www-authenticate"]


def test_snapshot_copy_cannot_gain_ignored_runtime_content(gateway_snapshot: Path):
    copied = gateway_snapshot.parent / "copy"
    shutil.copytree(gateway_snapshot, copied)
    (copied / "content/audit").mkdir()
    (copied / "content/audit/tool_calls.ndjson").write_text("{}\n", encoding="utf-8")
    with pytest.raises(GatewaySnapshotError, match="unapproved"):
        validate_gateway_snapshot(copied, require_read_only=False)


def test_gateway_snapshot_rejects_symlinks_and_hard_links(gateway_snapshot: Path):
    symlink = gateway_snapshot / "content/docs/linked.md"
    symlink.symlink_to(gateway_snapshot / "content/README.md")
    with pytest.raises(GatewaySnapshotError, match="symlink"):
        validate_gateway_snapshot(gateway_snapshot, require_read_only=False)
    symlink.unlink()

    hard_link = gateway_snapshot / "content/docs/copied.md"
    os.link(gateway_snapshot / "content/README.md", hard_link)
    with pytest.raises(GatewaySnapshotError, match="hard link|multiply linked"):
        validate_gateway_snapshot(gateway_snapshot, require_read_only=False)
