"""Remove old versioned snapshots after a healthy replacement is active."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
from pathlib import Path

COMMIT_DIRECTORY = re.compile(r"^[0-9a-f]{40}$")


def _force_writable(path: Path) -> None:
    """Restore owner write across a promoted snapshot tree before removal.

    Snapshots are published read-only (files ``0444``, directories ``0555``),
    so a plain ``shutil.rmtree`` cannot unlink their entries -- the read-only
    parent directories reject the unlink. Re-adding owner write/execute to the
    doomed tree lets it be removed without ever touching the snapshots that are
    being retained.
    """
    path.chmod(path.stat().st_mode | stat.S_IRWXU)
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames:
            child = os.path.join(dirpath, name)
            os.chmod(child, os.stat(child).st_mode | stat.S_IRWXU)
        for name in filenames:
            child = os.path.join(dirpath, name)
            os.chmod(child, os.stat(child).st_mode | stat.S_IWUSR)


def prune(snapshot_root: Path, current: Path, keep: int) -> list[str]:
    root = snapshot_root.resolve(strict=True)
    selected = current.resolve(strict=True)
    if selected.parent != root or not COMMIT_DIRECTORY.fullmatch(selected.name):
        raise ValueError("current snapshot is not a versioned child of the snapshot root")
    candidates: list[Path] = []
    for child in root.iterdir():
        metadata = child.lstat()
        if (
            stat.S_ISDIR(metadata.st_mode)
            and not stat.S_ISLNK(metadata.st_mode)
            and COMMIT_DIRECTORY.fullmatch(child.name)
        ):
            candidates.append(child)
    candidates.sort(
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    retained = set(candidates[:keep])
    retained.add(selected)
    removed: list[str] = []
    for candidate in candidates:
        if candidate in retained:
            continue
        _force_writable(candidate)
        shutil.rmtree(candidate)
        removed.append(candidate.name)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-root", required=True, type=Path)
    parser.add_argument("--current", required=True, type=Path)
    parser.add_argument("--keep", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.keep <= 10:
        raise ValueError("keep must be in 1..10")
    removed = prune(args.snapshot_root, args.current, args.keep)
    print(f"snapshot retention removed={len(removed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
