"""Validate a snapshot without exposing its host path."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.core.project_context import ProjectContextAssembler
from app.core.snapshot import validate_snapshot
from app.core.symbol_index import SymbolIndex
from app.core.task_index import TaskIndex


def validate_and_build(snapshot_path: Path, *, require_read_only: bool) -> dict[str, object]:
    snapshot = validate_snapshot(
        snapshot_path,
        require_read_only=require_read_only,
    )
    tasks = TaskIndex(snapshot)
    symbols = SymbolIndex(snapshot)
    ProjectContextAssembler(snapshot, tasks).build()
    return {
        "repository": snapshot.manifest.repository,
        "ref": snapshot.manifest.ref,
        "commit": snapshot.manifest.commit,
        "tree": snapshot.manifest.tree,
        "file_count": len(snapshot.files),
        "total_bytes": snapshot.manifest.total_bytes,
        "content_sha256": snapshot.manifest.content_sha256,
        "task_records": len(tasks.tasks),
        "decomp_rows": symbols.decomp_row_count,
        "class_id_rows": symbols.class_id_row_count,
        "linkage_rows": symbols.linkage_row_count,
        "symbol_records": len(symbols.records),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--allow-writable", action="store_true")
    args = parser.parse_args()
    result = validate_and_build(
        args.snapshot,
        require_read_only=not args.allow_writable,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
