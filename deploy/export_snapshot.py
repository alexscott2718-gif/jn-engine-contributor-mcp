"""Filter a fixed Git archive export and write its strict snapshot manifest."""

from __future__ import annotations

import argparse
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from app.config import EXPECTED_REF, EXPECTED_REPOSITORY
from app.core.snapshot import (
    APPROVED_ROOT_DIRECTORIES,
    APPROVED_ROOT_FILES,
    INCLUDED_ROOTS,
    MAX_FILE_BYTES,
    SnapshotError,
    SnapshotManifest,
    compute_content_inventory,
    is_admitted_repository_path,
    validate_snapshot,
)


def _reject_unsafe_archive_entries(content_root: Path) -> None:
    for current, directories, files in os.walk(content_root, followlinks=False):
        current_path = Path(current)
        for name in (*directories, *files):
            path = current_path / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise SnapshotError("Git archive contains a symlink")
            if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
                raise SnapshotError("Git archive contains a special file")
            if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
                raise SnapshotError("Git archive contains a hard link")


def _filter_export(content_root: Path) -> None:
    _reject_unsafe_archive_entries(content_root)
    for current, _directories, files in os.walk(content_root, topdown=False):
        current_path = Path(current)
        for filename in files:
            path = current_path / filename
            relative = path.relative_to(content_root).as_posix()
            if not is_admitted_repository_path(relative):
                path.unlink()
                continue
            metadata = path.stat()
            if metadata.st_size > MAX_FILE_BYTES:
                raise SnapshotError("admitted Git file exceeds the size bound")
            with path.open("rb") as handle:
                if b"\x00" in handle.read(4_096):
                    raise SnapshotError("admitted Git file is binary")
        for directory in list((current_path).iterdir()):
            if directory.is_dir() and not any(directory.iterdir()):
                relative = directory.relative_to(content_root).as_posix()
                if (
                    len(Path(relative).parts) > 1
                    or relative not in APPROVED_ROOT_DIRECTORIES
                ):
                    directory.rmdir()

    for root_file in APPROVED_ROOT_FILES:
        if not (content_root / root_file).is_file():
            raise SnapshotError(f"archive is missing required root file {root_file}")
    for root_directory in APPROVED_ROOT_DIRECTORIES:
        if not (content_root / root_directory).is_dir():
            raise SnapshotError(
                f"archive is missing required root directory {root_directory}"
            )


def build_manifest(
    snapshot_root: Path,
    *,
    commit: str,
    tree: str,
    commit_time: str,
) -> SnapshotManifest:
    content_root = snapshot_root / "content"
    _filter_export(content_root)
    inventory = compute_content_inventory(content_root)
    parsed_commit_time = datetime.fromisoformat(commit_time.replace("Z", "+00:00"))
    if parsed_commit_time.tzinfo is None:
        raise SnapshotError("Git commit time is missing its timezone")
    manifest = SnapshotManifest(
        schema_version=1,
        repository=EXPECTED_REPOSITORY,
        ref=EXPECTED_REF,
        commit=commit,
        tree=tree,
        commit_time=parsed_commit_time.astimezone(UTC),
        created_at=datetime.now(UTC),
        file_count=inventory.file_count,
        total_bytes=inventory.total_bytes,
        content_sha256=inventory.content_sha256,
        included_roots=INCLUDED_ROOTS,
    )
    manifest_path = snapshot_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            manifest.model_dump(mode="json"),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    validate_snapshot(snapshot_root, require_read_only=False)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tree", required=True)
    parser.add_argument("--commit-time", required=True)
    args = parser.parse_args()
    manifest = build_manifest(
        args.snapshot,
        commit=args.commit,
        tree=args.tree,
        commit_time=args.commit_time,
    )
    print(
        json.dumps(
            {
                "repository": manifest.repository,
                "ref": manifest.ref,
                "commit": manifest.commit,
                "tree": manifest.tree,
                "file_count": manifest.file_count,
                "total_bytes": manifest.total_bytes,
                "content_sha256": manifest.content_sha256,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
