"""Immutable snapshot manifest, corpus, digest, and health assembly tests."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.content_search import ContentSearch
from app.core.path_safety import StaleContentIdError
from app.core.snapshot import (
    MAX_FILE_BYTES,
    SnapshotError,
    compute_content_inventory,
    load_snapshot_manifest,
    validate_snapshot,
)
from app.main import create_app
from app.rest.health import create_health_router
from deploy.export_snapshot import build_manifest
from scripts.validate_snapshot import validate_and_build
from tests.conftest import GROUNDING_COMMIT

REAL_SNAPSHOT = Path(
    os.environ.get(
        "JN_TEST_SNAPSHOT_PATH",
        "/srv/jn-engine-contributor-mcp/test-snapshots/"
        "925242073a771aa68996c294aec8cc41cb43a0ef",
    )
)


def _rewrite_manifest(snapshot: Path, **changes: object) -> None:
    path = snapshot / "manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(changes)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _make_read_only(snapshot: Path) -> None:
    for current, directories, files in os.walk(snapshot):
        current_path = Path(current)
        for filename in files:
            (current_path / filename).chmod(0o444)
        for directory in directories:
            (current_path / directory).chmod(0o555)
    snapshot.chmod(0o555)


def test_manifest_is_strict_and_commit_is_exposed(snapshot: Path):
    manifest = load_snapshot_manifest(snapshot)
    assert manifest.commit == GROUNDING_COMMIT
    validated = validate_snapshot(snapshot, require_read_only=False)
    assert tuple(validated.files_by_path) == (
        "AGENTS.md",
        "Makefile",
        "README.md",
    )

    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    application.include_router(create_health_router(manifest.commit))
    with TestClient(application) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "app": "jn-engine-contributor-mcp",
            "mode": "read_only",
            "commit": GROUNDING_COMMIT,
        }
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_manifest_rejects_additional_keys(snapshot: Path):
    _rewrite_manifest(snapshot, unexpected=True)
    with pytest.raises(SnapshotError, match="manifest is invalid"):
        load_snapshot_manifest(snapshot)


def test_manifest_read_is_size_bounded(snapshot: Path):
    (snapshot / "manifest.json").write_text("{" + (" " * 65_536), encoding="utf-8")
    with pytest.raises(SnapshotError, match="size bound"):
        load_snapshot_manifest(snapshot)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", "someone/else"),
        ("ref", "refs/heads/main"),
        ("commit", "ABC"),
        ("tree", "1" * 39),
        ("schema_version", 2),
        ("included_roots", ["README.md"]),
    ],
)
def test_manifest_rejects_identity_or_schema_changes(
    snapshot: Path,
    field: str,
    value: object,
):
    _rewrite_manifest(snapshot, **{field: value})
    with pytest.raises(SnapshotError, match="manifest is invalid"):
        load_snapshot_manifest(snapshot)


@pytest.mark.parametrize(
    ("field", "delta", "message"),
    [
        ("file_count", 1, "file_count"),
        ("total_bytes", 1, "total_bytes"),
    ],
)
def test_manifest_inventory_values_are_recomputed(
    snapshot: Path,
    field: str,
    delta: int,
    message: str,
):
    payload = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    _rewrite_manifest(snapshot, **{field: payload[field] + delta})
    with pytest.raises(SnapshotError, match=message):
        validate_snapshot(snapshot, require_read_only=False)


def test_manifest_digest_is_recomputed(snapshot: Path):
    (snapshot / "content" / "README.md").write_text("XXXXXXXXX\n", encoding="utf-8")
    with pytest.raises(SnapshotError, match="content_sha256"):
        validate_snapshot(snapshot, require_read_only=False)


def test_digest_algorithm_matches_frozen_nul_delimited_contract(snapshot: Path):
    inventory = compute_content_inventory(snapshot / "content")
    expected = hashlib.sha256()
    for item in inventory.files:
        data = item.path.read_bytes()
        expected.update(item.relative_path.encode("utf-8"))
        expected.update(b"\0")
        expected.update(str(len(data)).encode("ascii"))
        expected.update(b"\0")
        expected.update(data)
        expected.update(b"\0")
    assert inventory.content_sha256 == expected.hexdigest()


def test_read_only_snapshot_is_required(snapshot: Path):
    with pytest.raises(SnapshotError, match="writable"):
        validate_snapshot(snapshot)
    _make_read_only(snapshot)
    validated = validate_snapshot(snapshot)
    assert validated.manifest.commit == GROUNDING_COMMIT


@pytest.mark.parametrize("missing", ["AGENTS.md", "README.md", "Makefile", "src"])
def test_required_roots_are_mandatory(snapshot: Path, missing: str):
    path = snapshot / "content" / missing
    if path.is_dir():
        path.rmdir()
    else:
        path.unlink()
    with pytest.raises(SnapshotError, match="required|missing"):
        validate_snapshot(snapshot, require_read_only=False)


def test_snapshot_root_symlink_is_rejected(snapshot: Path, tmp_path: Path):
    link = tmp_path / "linked-snapshot"
    link.symlink_to(snapshot, target_is_directory=True)
    with pytest.raises(SnapshotError, match="real directory"):
        validate_snapshot(link, require_read_only=False)


def test_content_symlink_is_rejected(snapshot: Path):
    (snapshot / "content" / "docs" / "escape.md").symlink_to("/etc/passwd")
    with pytest.raises(SnapshotError, match="symlink"):
        validate_snapshot(snapshot, require_read_only=False)


def test_hard_link_is_rejected(snapshot: Path):
    os.link(
        snapshot / "content" / "README.md",
        snapshot / "content" / "docs" / "copy.md",
    )
    with pytest.raises(SnapshotError, match="multiply linked|hard link"):
        validate_snapshot(snapshot, require_read_only=False)


def test_special_file_is_rejected(snapshot: Path):
    fifo = snapshot / "content" / "docs" / "pipe.md"
    os.mkfifo(fifo)
    with pytest.raises(SnapshotError, match="special file"):
        validate_snapshot(snapshot, require_read_only=False)


@pytest.mark.parametrize(
    "relative",
    [
        "docs/.hidden.md",
        "docs/image.png",
        "src/engine/stb_image.h",
        "web/grn-catalog/vendor/library.js",
    ],
)
def test_hidden_disallowed_and_vendored_files_are_rejected(
    snapshot: Path,
    relative: str,
):
    path = snapshot / "content" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not admitted\n", encoding="utf-8")
    with pytest.raises(SnapshotError, match="hidden|outside|excluded"):
        validate_snapshot(snapshot, require_read_only=False)


def test_unapproved_root_directory_is_rejected(snapshot: Path):
    path = snapshot / "content" / "assets"
    path.mkdir()
    (path / "note.md").write_text("no\n", encoding="utf-8")
    with pytest.raises(SnapshotError, match="unapproved root"):
        validate_snapshot(snapshot, require_read_only=False)


def test_binary_and_oversized_files_are_rejected(snapshot: Path):
    binary = snapshot / "content" / "docs" / "binary.md"
    binary.write_bytes(b"text\0binary")
    with pytest.raises(SnapshotError, match="binary"):
        validate_snapshot(snapshot, require_read_only=False)
    binary.unlink()

    oversized = snapshot / "content" / "docs" / "large.md"
    with oversized.open("wb") as handle:
        handle.truncate(MAX_FILE_BYTES + 1)
    with pytest.raises(SnapshotError, match="oversized"):
        validate_snapshot(snapshot, require_read_only=False)


def test_exporter_filters_outside_policy_and_writes_verified_manifest(
    snapshot: Path,
):
    root = snapshot
    (root / "manifest.json").unlink()
    (root / "content" / "docs" / "remove.png").write_bytes(b"png")
    vendor = root / "content" / "web" / "grn-catalog" / "vendor"
    vendor.mkdir(parents=True)
    (vendor / "remove.js").write_text("vendored\n", encoding="utf-8")

    manifest = build_manifest(
        root,
        commit=GROUNDING_COMMIT,
        tree="1" * 40,
        commit_time="2026-07-11T00:00:00Z",
    )
    assert manifest.file_count == 3
    assert not (root / "content" / "docs" / "remove.png").exists()
    assert not vendor.exists()
    validate_snapshot(root, require_read_only=False)


def test_verified_production_settings_build_complete_app(github_settings):
    values = github_settings.model_dump()
    values.update(
        {
            "app_env": "production",
            "jn_snapshot_path": REAL_SNAPSHOT,
            "public_base_url": "https://jn-ai.example",
            "oauth_allowed_client_redirect_uris": (
                "https://claude.ai/api/mcp/auth_callback",
            ),
        }
    )
    production = Settings(**values)
    application = create_app(production)

    assert application.state.settings.app_env == "production"
    assert application.state.data_plane.snapshot.manifest.commit == GROUNDING_COMMIT
    assert application.docs_url is None
    assert application.openapi_url is None


def test_ops_validator_builds_real_indexes_before_promotion():
    result = validate_and_build(REAL_SNAPSHOT, require_read_only=True)
    assert result["commit"] == GROUNDING_COMMIT
    assert result["file_count"] == 665
    assert result["decomp_rows"] == 208
    assert result["class_id_rows"] == 238
    assert result["linkage_rows"] == 29
    assert result["task_records"] == 267
    assert result["symbol_records"] == 2_500


def test_atomic_second_fixture_promotion_changes_process_view_and_stales_ids(
    tmp_path: Path,
):
    first = validate_snapshot(REAL_SNAPSHOT)
    first_search = ContentSearch(first, search_engine="python")
    old_id = first_search.search("clean-room", limit=1).results[0].id
    assert first_search.fetch(old_id).commit == GROUNDING_COMMIT

    root = tmp_path / "snapshots"
    root.mkdir()
    stage = root / ".staging-v2"
    target = root / ("2" * 40)
    shutil.copytree(REAL_SNAPSHOT, stage)
    (stage / "content/README.md").chmod(0o644)
    with (stage / "content/README.md").open("a", encoding="utf-8") as handle:
        handle.write("\nfixture-v2-marker\n")
    (stage / "manifest.json").chmod(0o644)
    inventory = compute_content_inventory(stage / "content")
    _rewrite_manifest(
        stage,
        commit="2" * 40,
        tree="3" * 40,
        file_count=inventory.file_count,
        total_bytes=inventory.total_bytes,
        content_sha256=inventory.content_sha256,
    )
    _make_read_only(stage)
    os.replace(stage, target)

    second = validate_snapshot(target)
    second_search = ContentSearch(second, search_engine="python")
    new_result = second_search.search("fixture-v2-marker", limit=1).results[0]
    assert new_result.id != old_id
    assert second_search.fetch(new_result.id).commit == "2" * 40
    with pytest.raises(StaleContentIdError):
        second_search.fetch(old_id)

    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    application.include_router(create_health_router(second.manifest.commit))
    with TestClient(application) as client:
        assert client.get("/health").json()["commit"] == "2" * 40


def test_prune_removes_read_only_snapshots(tmp_path: Path):
    """Promoted snapshots are read-only; prune must still be able to remove them."""
    from deploy.prune_snapshots import prune

    root = tmp_path / "snapshots"
    root.mkdir()
    commits = ["0" * 40, "1" * 40, "2" * 40, "3" * 40]
    made: list[Path] = []
    for index, name in enumerate(commits):
        snapshot = root / name
        (snapshot / "content").mkdir(parents=True)
        (snapshot / "manifest.json").write_text("{}", encoding="utf-8")
        (snapshot / "content" / "AGENTS.md").write_text("x", encoding="utf-8")
        os.utime(snapshot, (1_000 + index, 1_000 + index))
        made.append(snapshot)
    for snapshot in made:
        _make_read_only(snapshot)

    removed = prune(root, made[-1], keep=3)

    # Retain the three most recent by mtime plus the current one; drop the oldest.
    assert removed == ["0" * 40]
    assert not (root / ("0" * 40)).exists()
    for retained in commits[1:]:
        assert (root / retained).exists()
