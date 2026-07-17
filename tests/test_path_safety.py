"""Commit IDs, containment, negative paths, citations, and bounded reads."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from urllib.parse import quote

import pytest

from app.core.path_safety import (
    CONTENT_ID_PREFIX,
    MAX_FETCH_CHARS,
    ContentNotFoundError,
    ContentUnavailableError,
    InvalidContentIdError,
    StaleContentIdError,
    UnsafePathError,
    citation_url,
    decode_content_id,
    encode_content_id,
    language_for_path,
    read_text_bounded,
    resolve_content_id,
    resolve_relative_file,
    title_from_text,
    validate_relative_path,
)
from app.core.snapshot import compute_content_inventory, validate_snapshot
from tests.conftest import GROUNDING_COMMIT


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


def _add_file(snapshot: Path, relative: str, data: bytes) -> None:
    path = snapshot / "content" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    _refresh_manifest(snapshot)


def _encoded(raw: bytes) -> str:
    return CONTENT_ID_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_content_id_round_trip_and_exact_payload(snapshot: Path):
    _add_file(snapshot, "docs/My Note.md", b"# Grounded title\nbody\n")
    content_id = encode_content_id(GROUNDING_COMMIT, "docs/My Note.md")
    expected_payload = f"{GROUNDING_COMMIT}\0docs/My Note.md".encode()
    assert content_id == _encoded(expected_payload)
    assert decode_content_id(content_id).commit == GROUNDING_COMMIT
    assert decode_content_id(content_id).relative_path == "docs/My Note.md"

    active = validate_snapshot(snapshot, require_read_only=False)
    item = resolve_content_id(active, content_id)
    assert item.relative_path == "docs/My Note.md"


@pytest.mark.parametrize(
    "content_id",
    [
        "",
        "note_abc",
        "jn1_",
        "jn1_%%%%",
        "jn1_YQ==",
        "jn1_" + ("a" * 8_193),
    ],
)
def test_malformed_content_ids_are_rejected(content_id: str):
    with pytest.raises(InvalidContentIdError):
        decode_content_id(content_id)


@pytest.mark.parametrize(
    "payload",
    [
        b"no separator",
        b"a\0b\0c",
        b"f" * 39 + b"\0docs/a.md",
        b"G" * 40 + b"\0docs/a.md",
        b"f" * 40 + b"\0docs/\xff.md",
        b"f" * 40 + b"\0../docs/a.md",
    ],
)
def test_decoded_payload_contract_is_strict(payload: bytes):
    with pytest.raises(InvalidContentIdError):
        decode_content_id(_encoded(payload))


@pytest.mark.parametrize(
    "raw_path",
    [
        "",
        "/etc/passwd",
        "../README.md",
        "docs/../README.md",
        "docs/./note.md",
        "docs//note.md",
        "docs/note.md/",
        r"docs\ note.md",
        r"C:\Windows\win.ini",
        "C:/Windows/win.ini",
        "docs/.hidden.md",
        ".git/config",
        "docs/note.png",
        "assets/note.md",
        "src/engine/stb_image.h",
        "web/grn-catalog/vendor/library.js",
        "docs/nul\x00name.md",
    ],
)
def test_path_negative_matrix(raw_path: str):
    with pytest.raises(UnsafePathError):
        validate_relative_path(raw_path)


def test_stale_id_is_distinct_from_missing_and_invalid(snapshot: Path):
    active = validate_snapshot(snapshot, require_read_only=False)
    stale_id = encode_content_id("f" * 40, "README.md")
    with pytest.raises(StaleContentIdError):
        resolve_content_id(active, stale_id)

    missing_id = encode_content_id(GROUNDING_COMMIT, "docs/missing.md")
    with pytest.raises(ContentNotFoundError):
        resolve_content_id(active, missing_id)

    with pytest.raises(InvalidContentIdError):
        resolve_content_id(active, "jn1_not-valid!")


def test_open_time_symlink_escape_is_rejected(snapshot: Path):
    _add_file(snapshot, "docs/safe.md", b"safe\n")
    active = validate_snapshot(snapshot, require_read_only=False)
    target = snapshot / "content" / "docs" / "safe.md"
    target.unlink()
    target.symlink_to("/etc/passwd")
    with pytest.raises(ContentUnavailableError, match="symlink"):
        resolve_relative_file(active, "docs/safe.md")


def test_open_time_missing_and_hardlink_changes_are_rejected(snapshot: Path):
    _add_file(snapshot, "docs/safe.md", b"safe\n")
    active = validate_snapshot(snapshot, require_read_only=False)
    target = snapshot / "content" / "docs" / "safe.md"
    target.unlink()
    with pytest.raises(ContentNotFoundError):
        resolve_relative_file(active, "docs/safe.md")

    target.write_bytes(b"safe\n")
    linked = snapshot / "content" / "docs" / "linked.md"
    os.link(target, linked)
    with pytest.raises(ContentUnavailableError, match="single regular"):
        resolve_relative_file(active, "docs/safe.md")


def test_open_time_binary_and_size_changes_are_rejected(snapshot: Path):
    _add_file(snapshot, "docs/safe.md", b"safe\n")
    active = validate_snapshot(snapshot, require_read_only=False)
    item = active.file("docs/safe.md")
    assert item is not None
    item.path.write_bytes(b"x\0xx\n")
    with pytest.raises(ContentUnavailableError, match="binary"):
        read_text_bounded(active, item)

    item.path.write_bytes(b"changed length\n")
    with pytest.raises(ContentUnavailableError, match="changed"):
        read_text_bounded(active, item)


def test_read_is_utf8_replacement_and_character_bounded(snapshot: Path):
    text = ("x" * MAX_FETCH_CHARS) + "é" + "\ufffd"
    _add_file(snapshot, "docs/large.md", text.encode("utf-8") + b"\xff")
    active = validate_snapshot(snapshot, require_read_only=False)
    item = resolve_relative_file(active, "docs/large.md")
    returned, truncated = read_text_bounded(active, item)
    assert len(returned) == MAX_FETCH_CHARS
    assert returned == "x" * MAX_FETCH_CHARS
    assert truncated is True


def test_markdown_title_requires_first_nonempty_h1():
    assert title_from_text("docs/a.md", "\n# Title\nbody") == "Title"
    assert title_from_text("docs/a.md", "intro\n# Later") == "docs/a.md"
    assert title_from_text("docs/a.md", "## H2") == "docs/a.md"
    assert title_from_text("src/a.c", "# not markdown") == "src/a.c"
    assert len(title_from_text("docs/a.md", "# " + ("x" * 300))) == 200


def test_citation_is_commit_pinned_and_path_encoded():
    relative = "docs/My Note.md"
    expected = (
        "https://github.com/alexscott2718-gif/jn-engine/blob/"
        f"{GROUNDING_COMMIT}/{quote(relative, safe='/')}#L7"
    )
    assert citation_url(GROUNDING_COMMIT, relative, line=7) == expected
    assert "master" not in expected
    assert citation_url(GROUNDING_COMMIT, relative).endswith("docs/My%20Note.md")
    with pytest.raises(UnsafePathError):
        citation_url("not-a-commit", relative)
    with pytest.raises(UnsafePathError):
        citation_url(GROUNDING_COMMIT, relative, line=0)


@pytest.mark.parametrize(
    ("path", "language"),
    [
        ("README.md", "markdown"),
        ("src/a.c", "c"),
        ("src/a.h", "c"),
        ("tools/a.py", "python"),
        ("web/a.js", "javascript"),
        ("Makefile", None),
    ],
)
def test_language_is_deterministic(path: str, language: str | None):
    assert language_for_path(path) == language
