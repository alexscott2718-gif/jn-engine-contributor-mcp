"""Closed, immutable snapshot policy for the gateway repository itself."""

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

from app.config import EXPECTED_GATEWAY_REF, EXPECTED_GATEWAY_REPOSITORY

INCLUDED_ROOTS = (
    ".dockerignore",
    ".env.example",
    ".env.gateway-repo.example",
    ".github",
    ".gitignore",
    "CONTRIBUTING.md",
    "Dockerfile",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "app",
    "cloudflared",
    "deploy",
    "docker-compose.yml",
    "docker-compose.gateway-repo.yml",
    "docs",
    "pyproject.toml",
    "requirements.lock",
    "scripts",
    "tests",
)
APPROVED_ROOT_FILES = frozenset(
    (
        ".dockerignore",
        ".env.example",
        ".env.gateway-repo.example",
        ".gitignore",
        "CONTRIBUTING.md",
        "Dockerfile",
        "LICENSE",
        "README.md",
        "SECURITY.md",
        "docker-compose.yml",
        "docker-compose.gateway-repo.yml",
        "pyproject.toml",
        "requirements.lock",
    )
)
APPROVED_ROOT_DIRECTORIES = frozenset(
    (".github", "app", "cloudflared", "deploy", "docs", "scripts", "tests")
)
ALLOWED_SUFFIXES = frozenset(
    (
        ".css",
        ".html",
        ".json",
        ".lock",
        ".md",
        ".py",
        ".sh",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    )
)
MAX_FILE_BYTES = 1_000_000
MAX_SNAPSHOT_FILES = 5_000
MAX_SNAPSHOT_BYTES = 64_000_000
MAX_MANIFEST_BYTES = 65_536
BINARY_PROBE_BYTES = 4_096


class GatewaySnapshotError(RuntimeError):
    """The mounted gateway-repository snapshot cannot be trusted."""


class GatewaySnapshotManifest(BaseModel):
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
    def require_frozen_identity(self) -> "GatewaySnapshotManifest":
        if self.repository != EXPECTED_GATEWAY_REPOSITORY:
            raise ValueError("snapshot repository does not match the code constant")
        if self.ref != EXPECTED_GATEWAY_REF:
            raise ValueError("snapshot ref does not match the code constant")
        if self.included_roots != INCLUDED_ROOTS:
            raise ValueError("snapshot included_roots does not match the closed policy")
        return self


@dataclass(frozen=True)
class GatewaySnapshotFile:
    relative_path: str
    path: Path
    size_bytes: int


@dataclass(frozen=True)
class GatewayContentInventory:
    files: tuple[GatewaySnapshotFile, ...]
    file_count: int
    total_bytes: int
    content_sha256: str


@dataclass(frozen=True)
class GatewaySnapshot:
    root: Path
    content_root: Path
    manifest: GatewaySnapshotManifest
    files: tuple[GatewaySnapshotFile, ...]
    files_by_path: Mapping[str, GatewaySnapshotFile]

    def file(self, relative_path: str) -> GatewaySnapshotFile | None:
        return self.files_by_path.get(relative_path)


def is_admitted_gateway_path(relative_path: str) -> bool:
    if not relative_path or "\x00" in relative_path or "\\" in relative_path:
        return False
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        return False
    if len(relative.parts) == 1:
        return relative_path in APPROVED_ROOT_FILES
    root = relative.parts[0]
    if root not in APPROVED_ROOT_DIRECTORIES:
        return False
    if root == ".github":
        if len(relative.parts) < 3 or relative.parts[1] != "workflows":
            return False
        if any(part.startswith(".") for part in relative.parts[1:]):
            return False
        return relative.suffix.lower() in {".yaml", ".yml"}
    if any(part.startswith(".") for part in relative.parts):
        return False
    return relative.suffix.lower() in ALLOWED_SUFFIXES


def _require_real_directory(path: Path, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise GatewaySnapshotError(f"{label} is missing or inaccessible") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise GatewaySnapshotError(f"{label} must be a real directory")
    return metadata


def _require_regular_file(path: Path, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise GatewaySnapshotError(
            f"required snapshot file is missing: {label}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise GatewaySnapshotError(f"required snapshot file is unsafe: {label}")
    if metadata.st_nlink != 1:
        raise GatewaySnapshotError(
            f"required snapshot file is multiply linked: {label}"
        )
    return metadata


def load_gateway_manifest(snapshot_path: Path) -> GatewaySnapshotManifest:
    _require_real_directory(snapshot_path, "gateway snapshot root")
    manifest_path = snapshot_path / "manifest.json"
    metadata = _require_regular_file(manifest_path, "manifest.json")
    if metadata.st_size > MAX_MANIFEST_BYTES:
        raise GatewaySnapshotError("gateway snapshot manifest exceeds its size bound")
    try:
        payload = json.loads(manifest_path.read_bytes().decode("utf-8"))
        return GatewaySnapshotManifest.model_validate(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise GatewaySnapshotError("gateway snapshot manifest is invalid") from exc


def _scan_directory(
    content_root: Path,
    current: Path,
    relative_parts: tuple[str, ...],
    files: list[GatewaySnapshotFile],
) -> None:
    try:
        with os.scandir(current) as iterator:
            entries = sorted(
                iterator,
                key=lambda entry: entry.name.encode(
                    "utf-8", errors="surrogateescape"
                ),
            )
    except OSError as exc:
        raise GatewaySnapshotError("gateway snapshot cannot be enumerated") from exc
    for entry in entries:
        parts = (*relative_parts, entry.name)
        relative_path = PurePosixPath(*parts).as_posix()
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise GatewaySnapshotError(
                "gateway snapshot entry cannot be inspected"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise GatewaySnapshotError("gateway snapshot contains a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            if len(parts) == 1 and entry.name not in APPROVED_ROOT_DIRECTORIES:
                raise GatewaySnapshotError(
                    "gateway snapshot has an unapproved root directory"
                )
            _scan_directory(content_root, Path(entry.path), parts, files)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise GatewaySnapshotError("gateway snapshot contains a special file")
        if metadata.st_nlink != 1:
            raise GatewaySnapshotError("gateway snapshot contains a hard link")
        if not is_admitted_gateway_path(relative_path):
            raise GatewaySnapshotError(
                "gateway snapshot contains a file outside policy"
            )
        if metadata.st_size > MAX_FILE_BYTES:
            raise GatewaySnapshotError("gateway snapshot contains an oversized file")
        path = Path(entry.path)
        try:
            with path.open("rb") as handle:
                if b"\x00" in handle.read(BINARY_PROBE_BYTES):
                    raise GatewaySnapshotError("gateway snapshot contains a binary file")
        except OSError as exc:
            raise GatewaySnapshotError("gateway snapshot file cannot be read") from exc
        files.append(GatewaySnapshotFile(relative_path, path, metadata.st_size))


def compute_gateway_inventory(content_root: Path) -> GatewayContentInventory:
    _require_real_directory(content_root, "gateway snapshot content")
    for root_file in sorted(APPROVED_ROOT_FILES):
        _require_regular_file(content_root / root_file, root_file)
    for root_directory in sorted(APPROVED_ROOT_DIRECTORIES):
        _require_real_directory(content_root / root_directory, f"{root_directory}/")

    files: list[GatewaySnapshotFile] = []
    _scan_directory(content_root, content_root, (), files)
    files.sort(key=lambda item: item.relative_path.encode("utf-8"))
    if not files or len(files) > MAX_SNAPSHOT_FILES:
        raise GatewaySnapshotError("gateway snapshot file-count is outside bounds")
    total_bytes = sum(item.size_bytes for item in files)
    if total_bytes > MAX_SNAPSHOT_BYTES:
        raise GatewaySnapshotError("gateway snapshot exceeds the total-byte bound")

    digest = hashlib.sha256()
    for item in files:
        digest.update(item.relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item.size_bytes).encode("ascii"))
        digest.update(b"\0")
        try:
            with item.path.open("rb") as handle:
                while chunk := handle.read(64 * 1024):
                    digest.update(chunk)
        except OSError as exc:
            raise GatewaySnapshotError("gateway snapshot file cannot be hashed") from exc
        digest.update(b"\0")
    return GatewayContentInventory(tuple(files), len(files), total_bytes, digest.hexdigest())


def _require_nonwritable(snapshot_path: Path, inventory: GatewayContentInventory) -> None:
    paths = [snapshot_path, snapshot_path / "manifest.json", snapshot_path / "content"]
    paths.extend(item.path for item in inventory.files)
    for current, directories, _files in os.walk(snapshot_path / "content", followlinks=False):
        paths.extend(Path(current) / directory for directory in directories)
    for path in paths:
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise GatewaySnapshotError(
                "gateway snapshot permissions cannot be inspected"
            ) from exc
        if stat.S_IMODE(mode) & 0o222:
            raise GatewaySnapshotError("gateway snapshot is writable")


def validate_gateway_snapshot(
    snapshot_path: Path,
    *,
    require_read_only: bool = True,
) -> GatewaySnapshot:
    manifest = load_gateway_manifest(snapshot_path)
    content_root = snapshot_path / "content"
    inventory = compute_gateway_inventory(content_root)
    if manifest.file_count != inventory.file_count:
        raise GatewaySnapshotError("gateway snapshot file_count does not match")
    if manifest.total_bytes != inventory.total_bytes:
        raise GatewaySnapshotError("gateway snapshot total_bytes does not match")
    if manifest.content_sha256 != inventory.content_sha256:
        raise GatewaySnapshotError("gateway snapshot content_sha256 does not match")
    if require_read_only:
        _require_nonwritable(snapshot_path, inventory)
    return GatewaySnapshot(
        root=snapshot_path,
        content_root=content_root,
        manifest=manifest,
        files=inventory.files,
        files_by_path=MappingProxyType(
            {item.relative_path: item for item in inventory.files}
        ),
    )
