"""Filter a fixed Git archive and manifest the gateway repository corpus."""

from __future__ import annotations

import argparse
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from app.config import EXPECTED_GATEWAY_REF, EXPECTED_GATEWAY_REPOSITORY
from app.gateway_repo.snapshot import (
    APPROVED_ROOT_DIRECTORIES,
    APPROVED_ROOT_FILES,
    INCLUDED_ROOTS,
    MAX_FILE_BYTES,
    GatewaySnapshotError,
    GatewaySnapshotManifest,
    compute_gateway_inventory,
    is_admitted_gateway_path,
    validate_gateway_snapshot,
)


def _filter_export(content_root: Path) -> None:
    for current, directories, files in os.walk(content_root, topdown=False):
        current_path = Path(current)
        for name in (*directories, *files):
            metadata = (current_path / name).lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise GatewaySnapshotError("Git archive contains a symlink")
            if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
                raise GatewaySnapshotError("Git archive contains a special file")
        for filename in files:
            path = current_path / filename
            relative = path.relative_to(content_root).as_posix()
            if not is_admitted_gateway_path(relative):
                path.unlink()
                continue
            metadata = path.stat()
            if metadata.st_nlink != 1:
                raise GatewaySnapshotError("Git archive contains a hard link")
            if metadata.st_size > MAX_FILE_BYTES:
                raise GatewaySnapshotError("admitted Git file exceeds the size bound")
            with path.open("rb") as handle:
                if b"\x00" in handle.read(4_096):
                    raise GatewaySnapshotError("admitted Git file is binary")
        for directory in list(current_path.iterdir()):
            if directory.is_dir() and not any(directory.iterdir()):
                relative = directory.relative_to(content_root).as_posix()
                if len(Path(relative).parts) > 1 or relative not in APPROVED_ROOT_DIRECTORIES:
                    directory.rmdir()

    for root_file in APPROVED_ROOT_FILES:
        if not (content_root / root_file).is_file():
            raise GatewaySnapshotError(f"archive is missing required root file {root_file}")
    for root_directory in APPROVED_ROOT_DIRECTORIES:
        if not (content_root / root_directory).is_dir():
            raise GatewaySnapshotError(
                f"archive is missing required root directory {root_directory}"
            )


def build_gateway_manifest(
    snapshot_root: Path,
    *,
    commit: str,
    tree: str,
    commit_time: str,
) -> GatewaySnapshotManifest:
    content_root = snapshot_root / "content"
    _filter_export(content_root)
    inventory = compute_gateway_inventory(content_root)
    parsed_commit_time = datetime.fromisoformat(commit_time.replace("Z", "+00:00"))
    if parsed_commit_time.tzinfo is None:
        raise GatewaySnapshotError("Git commit time is missing its timezone")
    manifest = GatewaySnapshotManifest(
        schema_version=1,
        repository=EXPECTED_GATEWAY_REPOSITORY,
        ref=EXPECTED_GATEWAY_REF,
        commit=commit,
        tree=tree,
        commit_time=parsed_commit_time.astimezone(UTC),
        created_at=datetime.now(UTC),
        file_count=inventory.file_count,
        total_bytes=inventory.total_bytes,
        content_sha256=inventory.content_sha256,
        included_roots=INCLUDED_ROOTS,
    )
    (snapshot_root / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    validate_gateway_snapshot(snapshot_root, require_read_only=False)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tree", required=True)
    parser.add_argument("--commit-time", required=True)
    args = parser.parse_args()
    manifest = build_gateway_manifest(
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
