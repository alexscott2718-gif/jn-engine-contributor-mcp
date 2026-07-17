"""Literal search, scope classification, ordering, fetch, and engine parity."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.content_search import (
    MAX_MATCHES_PER_FILE,
    ContentKind,
    ContentSearch,
    SearchEngineError,
    SearchRequestError,
    SearchScope,
)
from app.core.path_safety import MAX_FETCH_CHARS
from app.core.snapshot import MAX_FILE_BYTES, compute_content_inventory, validate_snapshot
from tests.conftest import GROUNDING_COMMIT

REAL_SNAPSHOT = Path(
    os.environ.get(
        "JN_TEST_SNAPSHOT_PATH",
        "/srv/jn-engine-contributor-mcp/test-snapshots/"
        "925242073a771aa68996c294aec8cc41cb43a0ef",
    )
)


def _refresh_manifest(snapshot: Path) -> None:
    manifest_path = snapshot / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    inventory = compute_content_inventory(snapshot / "content")
    payload.update(
        file_count=inventory.file_count,
        total_bytes=inventory.total_bytes,
        content_sha256=inventory.content_sha256,
    )
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")


def _write(snapshot: Path, relative: str, text: str) -> None:
    path = snapshot / "content" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def search_snapshot(snapshot: Path) -> Path:
    _write(
        snapshot,
        "src/game/player.c",
        "literal multi match\nsecond LITERAL MULTI match\n--flag-query\ncomm.*stream\n",
    )
    _write(snapshot, "instrument/proxy/protocol.h", "literal multi protocol\n")
    _write(snapshot, "tools/ghidra/helper.py", "literal multi helper\n")
    _write(snapshot, "web/shell.js", "const label = 'literal multi';\n")
    _write(snapshot, "docs/guide.md", "# Contributor Guide\nliteral multi docs\n")
    _write(snapshot, "docs/exact.md", "# literal multi\nliteral multi exact title\n")
    _write(snapshot, "docs/needle.txt", "needle only in filename and content\n")
    _write(snapshot, "docs/other.txt", "needle only in content\n")
    _write(
        snapshot,
        "docs/decomp/C3DPlayer.md",
        "# C3DPlayer\nliteral multi reverse engineering\n",
    )
    _write(
        snapshot,
        "docs/decomp/_next_session.md",
        "# Next Session\nliteral multi active task\n",
    )
    boundary = snapshot / "content" / "docs" / "boundary.txt"
    boundary.write_bytes(
        (b"x" * (MAX_FILE_BYTES - len(b" boundary-token\n")))
        + b" boundary-token\n"
    )
    _refresh_manifest(snapshot)
    return snapshot


@pytest.fixture()
def search(search_snapshot: Path) -> ContentSearch:
    active = validate_snapshot(search_snapshot, require_read_only=False)
    return ContentSearch(active, search_engine="python")


def test_scope_membership_and_primary_kind(search: ContentSearch):
    by_path = {entry.item.relative_path: entry for entry in search.files}
    assert by_path["src/game/player.c"].kind is ContentKind.SOURCE
    assert by_path["src/game/player.c"].scopes == {
        SearchScope.ALL,
        SearchScope.SOURCE,
    }
    assert by_path["docs/guide.md"].kind is ContentKind.DOCUMENTATION
    assert SearchScope.DOCS in by_path["docs/guide.md"].scopes
    assert by_path["docs/decomp/C3DPlayer.md"].kind is ContentKind.REVERSE_ENGINEERING
    assert SearchScope.RE in by_path["docs/decomp/C3DPlayer.md"].scopes
    task = by_path["docs/decomp/_next_session.md"]
    assert task.kind is ContentKind.TASK
    assert {SearchScope.RE, SearchScope.TASKS}.issubset(task.scopes)
    protocol = by_path["instrument/proxy/protocol.h"]
    assert protocol.kind is ContentKind.REVERSE_ENGINEERING
    assert {SearchScope.SOURCE, SearchScope.RE}.issubset(protocol.scopes)


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ("source", {"src/game/player.c", "instrument/proxy/protocol.h", "tools/ghidra/helper.py", "web/shell.js"}),
        ("docs", {"docs/guide.md", "docs/exact.md"}),
        ("re", {"instrument/proxy/protocol.h", "tools/ghidra/helper.py", "docs/decomp/C3DPlayer.md", "docs/decomp/_next_session.md"}),
        ("tasks", {"docs/decomp/_next_session.md"}),
    ],
)
def test_closed_search_scopes(
    search: ContentSearch,
    scope: str,
    expected: set[str],
):
    paths = {result.path for result in search.search("literal multi", scope=scope).results}
    assert paths == expected


def test_search_shape_bounds_and_commit_pinned_citations(search: ContentSearch):
    response = search.search("literal multi", limit=3, include_content=True)
    assert response.query == "literal multi"
    assert response.scope is SearchScope.ALL
    assert response.count == len(response.results) == 3
    for result in response.results:
        assert result.id.startswith("jn1_")
        assert result.path.startswith(("docs/", "src/", "instrument/", "tools/", "web/"))
        assert GROUNDING_COMMIT in result.url
        assert result.url.endswith(f"#L{result.matches[0].line}")
        assert len(result.matches) <= MAX_MATCHES_PER_FILE
        assert all(len(match.text) <= 500 for match in result.matches)
        assert result.content is not None and len(result.content) <= 10_000


def test_deterministic_filename_title_and_kind_order(search: ContentSearch):
    exact = search.search("literal multi")
    assert exact.results[0].path == "docs/exact.md"

    ranked = search.search("needle")
    assert [result.path for result in ranked.results] == [
        "docs/needle.txt",
        "docs/other.txt",
    ]


@pytest.mark.parametrize("query", ["--flag-query", "comm.*stream"])
def test_flag_and_regex_looking_queries_are_literal(search: ContentSearch, query: str):
    response = search.search(query)
    assert [result.path for result in response.results] == ["src/game/player.c"]
    assert response.results[0].matches[0].text == query


def test_case_insensitive_multi_match(search: ContentSearch):
    result = next(
        result
        for result in search.search("LiTeRaL MuLtI").results
        if result.path == "src/game/player.c"
    )
    assert [match.line for match in result.matches] == [1, 2]


def test_file_exactly_at_size_boundary_is_searchable(search: ContentSearch):
    response = search.search("boundary-token", scope="docs")
    assert [result.path for result in response.results] == ["docs/boundary.txt"]
    assert len(response.results[0].matches[0].text) == 500


def test_post_start_unsafe_files_never_enter_either_engine(
    search: ContentSearch,
    search_snapshot: Path,
    tmp_path: Path,
):
    content = search_snapshot / "content"
    (content / "docs" / ".hidden.md").write_text("unsafe-marker", encoding="utf-8")
    (content / "docs" / "image.png").write_text("unsafe-marker", encoding="utf-8")
    (content / "docs" / "binary.md").write_bytes(b"unsafe-marker\0binary")
    outside = tmp_path / "outside.md"
    outside.write_text("unsafe-marker", encoding="utf-8")
    (content / "docs" / "escape.md").symlink_to(outside)

    assert search.search("unsafe-marker", engine="python").results == ()
    assert search.search("unsafe-marker", engine="ripgrep").results == ()


@pytest.mark.parametrize(
    ("query", "scope"),
    [
        ("literal multi", "all"),
        ("LITERAL MULTI", "source"),
        ("literal multi", "docs"),
        ("literal multi", "re"),
        ("literal multi", "tasks"),
        ("--flag-query", "all"),
        ("comm.*stream", "all"),
        ("boundary-token", "docs"),
        ("does-not-exist", "all"),
    ],
)
def test_python_and_ripgrep_are_byte_equivalent(
    search: ContentSearch,
    query: str,
    scope: str,
):
    assert shutil.which("rg") is not None
    python_result = search.search(
        query,
        scope=scope,
        include_content=True,
        engine="python",
    )
    ripgrep_result = search.search(
        query,
        scope=scope,
        include_content=True,
        engine="ripgrep",
    )
    assert python_result == ripgrep_result


def test_ripgrep_command_keeps_query_as_data(
    search: ContentSearch,
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr("app.core.content_search.subprocess.run", fake_run)
    search.search("--version", engine="ripgrep")
    command = captured["command"]
    assert isinstance(command, list)
    query_index = command.index("-e") + 1
    assert command[query_index] == "--version"
    assert command[query_index + 1] == "--"
    assert captured["shell"] is False
    assert captured["timeout"] == 10


def test_auto_falls_back_but_forced_ripgrep_fails(
    search: ContentSearch,
    monkeypatch: pytest.MonkeyPatch,
):
    def fail(*_args, **_kwargs):
        raise SearchEngineError("test failure")

    monkeypatch.setattr(search, "_ripgrep_line_map", fail)
    assert search.search("literal multi", engine="auto").count > 0
    with pytest.raises(SearchEngineError):
        search.search("literal multi", engine="ripgrep")


@pytest.mark.parametrize(
    ("query", "limit", "scope"),
    [
        ("", 20, "all"),
        (" " * 5, 20, "all"),
        ("x" * 201, 20, "all"),
        ("x", 0, "all"),
        ("x", 51, "all"),
        ("x", 20, "unknown"),
    ],
)
def test_search_request_bounds(query: str, limit: int, scope: str, search: ContentSearch):
    with pytest.raises(SearchRequestError):
        search.search(query, limit=limit, scope=scope)


def test_fetch_uses_same_inventory_id_and_bounded_metadata(search: ContentSearch):
    found = next(
        result
        for result in search.search("C3DPlayer").results
        if result.path == "docs/decomp/C3DPlayer.md"
    )
    fetched = search.fetch(found.id)
    assert fetched.path == found.path
    assert fetched.title == "C3DPlayer"
    assert fetched.kind is ContentKind.REVERSE_ENGINEERING
    assert fetched.language == "markdown"
    assert fetched.repository == "alexscott2718-gif/jn-engine"
    assert fetched.ref == "refs/heads/master"
    assert fetched.commit == GROUNDING_COMMIT
    assert fetched.text_chars == len(fetched.text)
    assert fetched.truncated is False
    assert "#L" not in fetched.url

    boundary = search.search("boundary-token").results[0]
    bounded = search.fetch(boundary.id)
    assert len(bounded.text) == MAX_FETCH_CHARS
    assert bounded.truncated is True


def test_real_snapshot_search_fetch_and_engine_parity():
    assert REAL_SNAPSHOT.is_dir(), (
        "real snapshot missing; run deploy/refresh_snapshot.sh --build-only"
    )
    active = validate_snapshot(REAL_SNAPSHOT)
    search = ContentSearch(active, search_engine="auto")
    python_result = search.search(
        "C3DPlayer",
        scope="re",
        engine="python",
        limit=10,
    )
    ripgrep_result = search.search(
        "C3DPlayer",
        scope="re",
        engine="ripgrep",
        limit=10,
    )
    assert python_result == ripgrep_result
    class_doc = next(
        result
        for result in python_result.results
        if result.path == "docs/decomp/C3DPlayer.md"
    )
    fetched = search.fetch(class_doc.id)
    assert fetched.commit == GROUNDING_COMMIT
    assert "C3DPlayer" in fetched.text
    assert "/home/" not in repr(python_result)
