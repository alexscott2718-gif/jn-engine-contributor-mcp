"""Commit-bound content IDs, path containment, citations, and bounded reads."""

from __future__ import annotations

import base64
import binascii
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import quote

from app.config import EXPECTED_REPOSITORY
from app.core.snapshot import (
    BINARY_PROBE_BYTES,
    MAX_FILE_BYTES,
    Snapshot,
    SnapshotFile,
    is_admitted_repository_path,
)

CONTENT_ID_PREFIX = "jn1_"
MAX_CONTENT_ID_CHARS = 8_192
MAX_FETCH_CHARS = 200_000
TITLE_CHARS = 200
GITHUB_BLOB_BASE = f"https://github.com/{EXPECTED_REPOSITORY}/blob"
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
PAYLOAD_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
H1_PATTERN = re.compile(r"^#(?:[ \t]+)(.+?)\s*$")


class ContentPathError(ValueError):
    """Base class for expected content-address failures."""


class UnsafePathError(ContentPathError):
    """A repository-relative path failed the closed policy."""


class InvalidContentIdError(ContentPathError):
    """A content ID cannot be decoded under the jn1 contract."""


class StaleContentIdError(ContentPathError):
    """A valid content ID belongs to a different snapshot commit."""


class ContentNotFoundError(FileNotFoundError):
    """A safe path is not present in the immutable inventory."""


class ContentUnavailableError(RuntimeError):
    """An inventoried file changed or became unsafe after startup."""


@dataclass(frozen=True)
class DecodedContentId:
    commit: str
    relative_path: str


def validate_relative_path(raw_path: str) -> str:
    """Validate one repository-relative POSIX file path without opening it."""
    if not raw_path or "\x00" in raw_path:
        raise UnsafePathError("path must be nonempty and contain no NUL")
    if "\\" in raw_path:
        raise UnsafePathError("backslashes are not allowed")
    if "//" in raw_path:
        raise UnsafePathError("empty path segments are not allowed")
    raw_segments = raw_path.split("/")
    posix = PurePosixPath(raw_path)
    windows = PureWindowsPath(raw_path)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise UnsafePathError("absolute paths and drive letters are not allowed")
    if any(part in {"", ".", ".."} for part in raw_segments):
        raise UnsafePathError("dot path segments are not allowed")
    if any(part.startswith(".") for part in raw_segments):
        raise UnsafePathError("hidden paths are not allowed")
    if not is_admitted_repository_path(raw_path):
        raise UnsafePathError("path is outside the admitted content policy")
    return posix.as_posix()


def encode_content_id(commit: str, relative_path: str) -> str:
    if not COMMIT_PATTERN.fullmatch(commit):
        raise InvalidContentIdError("commit is not a lowercase 40-character hash")
    normalized = validate_relative_path(relative_path)
    payload = f"{commit}\x00{normalized}".encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{CONTENT_ID_PREFIX}{encoded}"


def decode_content_id(content_id: str) -> DecodedContentId:
    if (
        not content_id.startswith(CONTENT_ID_PREFIX)
        or len(content_id) > MAX_CONTENT_ID_CHARS
    ):
        raise InvalidContentIdError("unknown or oversized content ID")
    encoded = content_id[len(CONTENT_ID_PREFIX) :]
    if not encoded or not PAYLOAD_PATTERN.fullmatch(encoded):
        raise InvalidContentIdError("content ID payload is not unpadded base64url")
    try:
        raw = base64.b64decode(
            encoded + ("=" * (-len(encoded) % 4)),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise InvalidContentIdError("content ID payload is malformed") from exc
    if raw.count(b"\x00") != 1:
        raise InvalidContentIdError("content ID payload must contain one separator")
    commit_bytes, path_bytes = raw.split(b"\x00", maxsplit=1)
    try:
        commit = commit_bytes.decode("ascii")
        relative_path = path_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidContentIdError("content ID payload is not valid UTF-8") from exc
    if not COMMIT_PATTERN.fullmatch(commit):
        raise InvalidContentIdError("content ID commit is malformed")
    try:
        normalized = validate_relative_path(relative_path)
    except UnsafePathError as exc:
        raise InvalidContentIdError("content ID path is unsafe") from exc
    return DecodedContentId(commit=commit, relative_path=normalized)


def _revalidate_file(snapshot: Snapshot, item: SnapshotFile) -> Path:
    root = snapshot.content_root
    current = root
    for index, part in enumerate(PurePosixPath(item.relative_path).parts):
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise ContentNotFoundError("content file is missing") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ContentUnavailableError("content path became a symlink")
        is_final = index == len(PurePosixPath(item.relative_path).parts) - 1
        if is_final:
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ContentUnavailableError("content file is no longer a single regular file")
            if metadata.st_size > MAX_FILE_BYTES:
                raise ContentUnavailableError("content file exceeds its size bound")
            if metadata.st_size != item.size_bytes:
                raise ContentUnavailableError("content file changed after startup")
        elif not stat.S_ISDIR(metadata.st_mode):
            raise ContentUnavailableError("content path component is not a directory")
    try:
        resolved_root = root.resolve(strict=True)
        resolved = current.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise ContentUnavailableError("content path failed containment") from exc
    return resolved


def resolve_relative_file(snapshot: Snapshot, raw_path: str) -> SnapshotFile:
    relative_path = validate_relative_path(raw_path)
    item = snapshot.file(relative_path)
    if item is None:
        raise ContentNotFoundError("content file is not in the active snapshot")
    _revalidate_file(snapshot, item)
    return item


def resolve_content_id(snapshot: Snapshot, content_id: str) -> SnapshotFile:
    decoded = decode_content_id(content_id)
    if decoded.commit != snapshot.manifest.commit:
        raise StaleContentIdError("content ID belongs to a different snapshot")
    return resolve_relative_file(snapshot, decoded.relative_path)


def read_text_bounded(
    snapshot: Snapshot,
    item: SnapshotFile,
    *,
    max_chars: int = MAX_FETCH_CHARS,
) -> tuple[str, bool]:
    """Open a revalidated file without following a final-component symlink."""
    path = _revalidate_file(snapshot, item)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ContentUnavailableError("content file cannot be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size != item.size_bytes
            or metadata.st_size > MAX_FILE_BYTES
        ):
            raise ContentUnavailableError("content file changed during open")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(data) > MAX_FILE_BYTES:
        raise ContentUnavailableError("content file exceeds its size bound")
    if b"\x00" in data[:BINARY_PROBE_BYTES]:
        raise ContentUnavailableError("content file is binary")
    text = data.decode("utf-8", errors="replace")
    truncated = len(text) > max_chars
    return text[:max_chars], truncated


def title_from_text(relative_path: str, text: str) -> str:
    if PurePosixPath(relative_path).suffix.lower() == ".md":
        for line in text.splitlines():
            if not line.strip():
                continue
            match = H1_PATTERN.fullmatch(line.strip())
            if match:
                return match.group(1).strip()[:TITLE_CHARS]
            break
    return relative_path[:TITLE_CHARS]


def citation_url(commit: str, relative_path: str, *, line: int | None = None) -> str:
    if not COMMIT_PATTERN.fullmatch(commit):
        raise UnsafePathError("citation commit is invalid")
    normalized = validate_relative_path(relative_path)
    encoded_path = quote(normalized, safe="/")
    url = f"{GITHUB_BLOB_BASE}/{commit}/{encoded_path}"
    if line is not None:
        if line < 1:
            raise UnsafePathError("citation line must be positive")
        url = f"{url}#L{line}"
    return url


def language_for_path(relative_path: str) -> str | None:
    suffix = PurePosixPath(relative_path).suffix.lower()
    return {
        ".md": "markdown",
        ".txt": "text",
        ".rst": "restructuredtext",
        ".c": "c",
        ".h": "c",
        ".cc": "cpp",
        ".cpp": "cpp",
        ".py": "python",
        ".java": "java",
        ".sh": "shell",
        ".js": "javascript",
        ".css": "css",
        ".html": "html",
        ".toml": "toml",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".csv": "csv",
        ".tsv": "tsv",
    }.get(suffix)
