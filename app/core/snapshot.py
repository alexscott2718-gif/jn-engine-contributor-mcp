"""Validation and immutable inventory for one exported JN Engine commit."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import EXPECTED_REF, EXPECTED_REPOSITORY

INCLUDED_ROOTS = (
    "AGENTS.md",
    "README.md",
    "Makefile",
    "src",
    "instrument",
    "tools",
    "docs",
    "web",
)
APPROVED_ROOT_FILES = frozenset(("AGENTS.md", "README.md", "Makefile"))
APPROVED_ROOT_DIRECTORIES = frozenset(("src", "instrument", "tools", "docs", "web"))
ALLOWED_SUFFIXES = frozenset(
    (
        ".md",
        ".txt",
        ".rst",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".py",
        ".java",
        ".sh",
        ".js",
        ".css",
        ".html",
        ".toml",
        ".json",
        ".yaml",
        ".yml",
        ".csv",
        ".tsv",
    )
)
EXCLUDED_FILES = frozenset(
    (
        "src/engine/stb_image.h",
        "src/engine/glad.c",
        "src/engine/glad.h",
        "src/engine/cgltf.h",
    )
)
EXCLUDED_PREFIXES = ("web/grn-catalog/vendor/",)

MAX_FILE_BYTES = 1_000_000
MAX_SNAPSHOT_FILES = 20_000
MAX_SNAPSHOT_BYTES = 128_000_000
MAX_MANIFEST_BYTES = 65_536
BINARY_PROBE_BYTES = 4_096


class SnapshotError(RuntimeError):
    """The mounted snapshot cannot be trusted."""


class SnapshotManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    repository: str
    ref: str
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    commit_time: datetime
    created_at: datetime
    file_count: int = Field(ge=1, le=MAX_SNAPSHOT_FILES)
    total_bytes: int = Field(ge=1, le=MAX_SNAPSHOT_BYTES)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    included_roots: tuple[str, ...]

    @field_validator("commit_time", "created_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("snapshot timestamps must be RFC 3339 UTC values")
        return value

    @model_validator(mode="after")
    def require_frozen_identity(self) -> "SnapshotManifest":
        if self.repository != EXPECTED_REPOSITORY:
            raise ValueError("snapshot repository does not match the code constant")
        if self.ref != EXPECTED_REF:
            raise ValueError("snapshot ref does not match the code constant")
        if self.included_roots != INCLUDED_ROOTS:
            raise ValueError("snapshot included_roots does not match the closed policy")
        return self


@dataclass(frozen=True)
class SnapshotFile:
    relative_path: str
    path: Path
    size_bytes: int


@dataclass(frozen=True)
class ContentInventory:
    files: tuple[SnapshotFile, ...]
    file_count: int
    total_bytes: int
    content_sha256: str


@dataclass(frozen=True)
class Snapshot:
    root: Path
    content_root: Path
    manifest: SnapshotManifest
    files: tuple[SnapshotFile, ...]
    files_by_path: Mapping[str, SnapshotFile]

    def file(self, relative_path: str) -> SnapshotFile | None:
        return self.files_by_path.get(relative_path)


def is_admitted_repository_path(relative_path: str) -> bool:
    """Return whether a POSIX repository path belongs to the frozen corpus."""
    if (
        not relative_path
        or "\x00" in relative_path
        or "\\" in relative_path
    ):
        return False
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or any(
        part in {"", ".", ".."} or part.startswith(".") for part in relative.parts
    ):
        return False
    if relative_path in EXCLUDED_FILES or any(
        relative_path.startswith(prefix) for prefix in EXCLUDED_PREFIXES
    ):
        return False
    if len(relative.parts) == 1:
        return relative_path in APPROVED_ROOT_FILES
    if relative.parts[0] not in APPROVED_ROOT_DIRECTORIES:
        return False
    return relative.suffix.lower() in ALLOWED_SUFFIXES


def _require_real_directory(path: Path, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SnapshotError(f"{label} is missing or inaccessible") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SnapshotError(f"{label} must be a real directory")
    return metadata


def _require_real_regular_file(path: Path, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SnapshotError(f"required snapshot file is missing: {label}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SnapshotError(f"required snapshot file is unsafe: {label}")
    if metadata.st_nlink != 1:
        raise SnapshotError(f"required snapshot file is multiply linked: {label}")
    return metadata


def load_snapshot_manifest(snapshot_path: Path) -> SnapshotManifest:
    """Parse the strict manifest without following a snapshot-root symlink."""
    _require_real_directory(snapshot_path, "snapshot root")
    manifest_path = snapshot_path / "manifest.json"
    metadata = _require_real_regular_file(manifest_path, "manifest.json")
    if metadata.st_size > MAX_MANIFEST_BYTES:
        raise SnapshotError("snapshot manifest exceeds its size bound")
    try:
        with manifest_path.open("rb") as handle:
            payload = json.loads(handle.read(MAX_MANIFEST_BYTES + 1).decode("utf-8"))
        return SnapshotManifest.model_validate(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise SnapshotError("snapshot manifest is invalid") from exc


def _scan_directory(
    content_root: Path,
    current: Path,
    relative_parts: tuple[str, ...],
    files: list[SnapshotFile],
    directories: list[Path],
) -> None:
    try:
        with os.scandir(current) as iterator:
            entries = sorted(
                iterator,
                key=lambda entry: entry.name.encode(
                    "utf-8",
                    errors="surrogateescape",
                ),
            )
    except OSError as exc:
        raise SnapshotError("snapshot directory cannot be enumerated") from exc

    for entry in entries:
        name = entry.name
        if name.startswith("."):
            raise SnapshotError("snapshot contains a hidden path")
        parts = (*relative_parts, name)
        relative_path = PurePosixPath(*parts).as_posix()
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise SnapshotError("snapshot entry cannot be inspected") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise SnapshotError("snapshot contains a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            if len(parts) == 1 and name not in APPROVED_ROOT_DIRECTORIES:
                raise SnapshotError("snapshot contains an unapproved root directory")
            if relative_path in {
                prefix.rstrip("/") for prefix in EXCLUDED_PREFIXES
            }:
                raise SnapshotError("snapshot contains an excluded vendored directory")
            directory = Path(entry.path)
            directories.append(directory)
            _scan_directory(
                content_root,
                directory,
                parts,
                files,
                directories,
            )
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise SnapshotError("snapshot contains a special file")
        if metadata.st_nlink != 1:
            raise SnapshotError("snapshot contains a hard link")
        if not is_admitted_repository_path(relative_path):
            raise SnapshotError("snapshot contains a file outside the admitted policy")
        if metadata.st_size > MAX_FILE_BYTES:
            raise SnapshotError("snapshot contains an oversized file")
        path = Path(entry.path)
        try:
            with path.open("rb") as handle:
                if b"\x00" in handle.read(BINARY_PROBE_BYTES):
                    raise SnapshotError("snapshot contains a binary file")
        except OSError as exc:
            raise SnapshotError("snapshot file cannot be read") from exc
        files.append(
            SnapshotFile(
                relative_path=relative_path,
                path=path,
                size_bytes=metadata.st_size,
            )
        )


def compute_content_inventory(content_root: Path) -> ContentInventory:
    """Validate the corpus policy and compute its deterministic manifest values."""
    _require_real_directory(content_root, "snapshot content")
    for root_file in sorted(APPROVED_ROOT_FILES):
        _require_real_regular_file(content_root / root_file, root_file)
    for root_directory in sorted(APPROVED_ROOT_DIRECTORIES):
        _require_real_directory(
            content_root / root_directory,
            f"{root_directory}/",
        )

    files: list[SnapshotFile] = []
    directories = [content_root]
    _scan_directory(content_root, content_root, (), files, directories)
    files.sort(key=lambda item: item.relative_path.encode("utf-8"))

    if not files:
        raise SnapshotError("snapshot contains no admitted files")
    if len(files) > MAX_SNAPSHOT_FILES:
        raise SnapshotError("snapshot exceeds the file-count bound")

    total_bytes = sum(item.size_bytes for item in files)
    if total_bytes > MAX_SNAPSHOT_BYTES:
        raise SnapshotError("snapshot exceeds the total-byte bound")

    digest = hashlib.sha256()
    for item in files:
        try:
            path_bytes = item.relative_path.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise SnapshotError("snapshot path is not valid UTF-8") from exc
        digest.update(path_bytes)
        digest.update(b"\x00")
        digest.update(str(item.size_bytes).encode("ascii"))
        digest.update(b"\x00")
        try:
            with item.path.open("rb") as handle:
                while chunk := handle.read(64 * 1024):
                    digest.update(chunk)
        except OSError as exc:
            raise SnapshotError("snapshot file cannot be hashed") from exc
        digest.update(b"\x00")

    return ContentInventory(
        files=tuple(files),
        file_count=len(files),
        total_bytes=total_bytes,
        content_sha256=digest.hexdigest(),
    )


def _require_nonwritable(
    snapshot_path: Path,
    content_root: Path,
    inventory: ContentInventory,
) -> None:
    paths = [snapshot_path, snapshot_path / "manifest.json", content_root]
    paths.extend(item.path for item in inventory.files)
    for current, directories, _files in os.walk(content_root, followlinks=False):
        paths.extend(Path(current) / directory for directory in directories)
    for path in paths:
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise SnapshotError("snapshot permissions cannot be inspected") from exc
        if stat.S_IMODE(mode) & 0o222:
            raise SnapshotError("snapshot is writable")


def validate_snapshot(
    snapshot_path: Path,
    *,
    require_read_only: bool = True,
) -> Snapshot:
    """Fully validate one snapshot and return its immutable, process-owned view."""
    manifest = load_snapshot_manifest(snapshot_path)
    content_root = snapshot_path / "content"
    inventory = compute_content_inventory(content_root)

    if manifest.file_count != inventory.file_count:
        raise SnapshotError("snapshot file_count does not match the corpus")
    if manifest.total_bytes != inventory.total_bytes:
        raise SnapshotError("snapshot total_bytes does not match the corpus")
    if manifest.content_sha256 != inventory.content_sha256:
        raise SnapshotError("snapshot content_sha256 does not match the corpus")
    if require_read_only:
        _require_nonwritable(snapshot_path, content_root, inventory)

    by_path = MappingProxyType(
        {item.relative_path: item for item in inventory.files}
    )
    return Snapshot(
        root=snapshot_path,
        content_root=content_root,
        manifest=manifest,
        files=inventory.files,
        files_by_path=by_path,
    )
