"""Five-source task parsing, status normalization, sorting, and grounding."""

from __future__ import annotations

import json
import logging
import os
import socket
from collections import Counter
from pathlib import Path

import pytest

from app.core.snapshot import compute_content_inventory, validate_snapshot
from app.core.task_index import (
    CATALOG_PATH,
    DECOMP_HEADERS,
    DECOMP_PATH,
    HANDOFF_PATH,
    LINKAGE_HEADERS,
    LINKAGE_PATH,
    QA_PATH,
    TaskIndex,
    TaskIndexError,
    TaskRequestError,
    TaskSourceKind,
    TaskStatus,
    normalize_qa_status,
)
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
def task_snapshot(snapshot: Path) -> Path:
    _write(
        snapshot,
        HANDOFF_PATH,
        """# Handoff
## Your task this session: live options
- (a) Open handoff option
- [x] Finished handoff option
## Hard rules
- not a task
""",
    )
    _write(
        snapshot,
        QA_PATH,
        """# QA
## Master report ledger (deduped ~24 unique)
| # | Level | Model | Cat | Issue | Group | Status |
|---|-------|-------|-----|-------|-------|--------|
| 1 | l1 | A | GFX | open issue | A | NOT FIXED |
| 2 | l2 | B | OTH | fixed issue | B | FIXED |
| 3 | l3 | C | OTH | blocked issue | C | INCOMPLETE / DEFERRED |
| 4 | l4 | D | OTH | wontfix issue | D | WONTFIX-AS-BUG; DEFERRED prose |
| 5 | l5 | E | OTH | unknown private detail | E | TODO |
## Done
""",
    )
    _write(
        snapshot,
        LINKAGE_PATH,
        ",".join(LINKAGE_HEADERS)
        + "\n"
        + "CLinked,aspect-a,domain-a,linked,oracle.py,docs/decomp/CLinked.md,done\n"
        + "CBlocked,aspect-b,domain-b,linked-blocked,,docs/decomp/CBlocked.md,"
        + ("x" * 1_200)
        + "\n",
    )
    decomp_rows = [
        (
            "COpen",
            "todo",
        ),
        (
            "CProgress",
            "in_progress",
        ),
        (
            "CSpec",
            "spec",
        ),
        (
            "CPorted",
            "ported",
        ),
        (
            "COptional",
            "ported(optional)",
        ),
        (
            "CValidated",
            "validated",
        ),
        (
            "CUnknown",
            "mystery",
        ),
    ]
    decomp = [",".join(DECOMP_HEADERS)]
    for class_name, status in decomp_rows:
        decomp.append(
            f"{class_name},base,00400000,,1,family,1,{status},owner,High,notes"
        )
    _write(snapshot, DECOMP_PATH, "\n".join(decomp) + "\n")
    _write(
        snapshot,
        CATALOG_PATH,
        """# Catalog
## Full Missing Native Behavior Queue
| Rank | Score | FourCC | Class | Family | Inst | Lvls | Visual | Levels |
|---:|---:|---|---|---|---:|---:|---|---|
| 1 | 10 | 3AAA | CAlpha | enemies | 2 | 5 | missing | l1 |
## Enemy-Family Specs With No Current \x60.gam\x60 Placement
| FourCC | Class | Visual status | Decomp doc |
|---|---|---|---|
| \x603BBB\x60 | \x60CBeta\x60 | unused | docs/decomp/CBeta.md |
""",
    )
    _write(
        snapshot,
        "docs/github_issues.md",
        "# GitHub Issues\nSECRET-ISSUE must never become a task\n",
    )
    _refresh_manifest(snapshot)
    return snapshot


@pytest.fixture()
def index(task_snapshot: Path) -> TaskIndex:
    return TaskIndex(validate_snapshot(task_snapshot, require_read_only=False))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("NOT FIXED", TaskStatus.OPEN),
        ("FIXED", TaskStatus.DONE),
        ("OPEN", TaskStatus.OPEN),
        ("PENDING", TaskStatus.OPEN),
        ("INCOMPLETE", TaskStatus.BLOCKED),
        ("DEFERRED", TaskStatus.BLOCKED),
        ("BLOCKED", TaskStatus.BLOCKED),
        ("DONE", TaskStatus.DONE),
        ("CLOSED", TaskStatus.DONE),
        ("RESOLVED", TaskStatus.DONE),
        ("WONTFIX-AS-BUG; DEFERRED prose", TaskStatus.DONE),
        ("TODO", None),
        ("VALIDATED", None),
    ],
)
def test_qa_status_normalization_is_specific_first(
    raw: str,
    expected: TaskStatus | None,
):
    assert normalize_qa_status(raw) is expected


def test_every_allowlisted_parser_contributes_expected_records(index: TaskIndex):
    by_source = Counter(task.source_kind for task in index.tasks)
    assert by_source == {
        TaskSourceKind.HANDOFF: 2,
        TaskSourceKind.QA: 4,
        TaskSourceKind.LINKAGE: 2,
        TaskSourceKind.DECOMP: 6,
        TaskSourceKind.CATALOG: 2,
    }
    assert {task.id for task in index.tasks}.issuperset(
        {
            "handoff:a",
            "qa:1",
            "linkage:clinked:aspect-a",
            "decomp:cspec",
            "catalog:3aaa:calpha",
        }
    )


def test_recommended_next_campaign_accepts_numbered_tasks(task_snapshot: Path):
    _write(
        task_snapshot,
        HANDOFF_PATH,
        """# Next Session
## What just landed
The portable collaboration campaign is complete.
## Recommended next campaign: gateway collaboration tools
1. `open_pr`: open a protected pull request.
2. `check_status`: report both required checks.
3. `request_ground_truth`: record capture-only work.
## Standard validation
Keep the public snapshot deterministic.
## Definition of done for the next session
Land one focused pull request.
""",
    )
    _refresh_manifest(task_snapshot)
    parsed = TaskIndex(validate_snapshot(task_snapshot, require_read_only=False))
    handoff = [
        task for task in parsed.tasks if task.source_kind is TaskSourceKind.HANDOFF
    ]
    assert [task.title for task in handoff] == [
        "open_pr: open a protected pull request.",
        "check_status: report both required checks.",
        "request_ground_truth: record capture-only work.",
    ]
    assert all(task.status is TaskStatus.OPEN for task in handoff)


def test_unknown_rows_are_skipped_with_sanitized_log(
    task_snapshot: Path,
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(logging.INFO)
    TaskIndex(validate_snapshot(task_snapshot, require_read_only=False))
    messages = [record.getMessage() for record in caplog.records]
    assert any(f"path={QA_PATH}" in message and "reason=unknown_status" in message for message in messages)
    assert any(f"path={DECOMP_PATH}" in message and "reason=unknown_status" in message for message in messages)
    assert all("unknown private detail" not in message for message in messages)


def test_sorting_filtering_limits_and_bounds(index: TaskIndex):
    status_rank = {TaskStatus.OPEN: 0, TaskStatus.BLOCKED: 1, TaskStatus.DONE: 2}
    assert [status_rank[task.status] for task in index.tasks] == sorted(
        status_rank[task.status] for task in index.tasks
    )
    open_tasks = index.list_tasks(status="open", source="all", limit=100)
    assert open_tasks.count == len(open_tasks.tasks)
    assert all(task.status is TaskStatus.OPEN for task in open_tasks.tasks)
    linkage = index.list_tasks(status="all", source="linkage", limit=1)
    assert linkage.count == 1
    assert linkage.tasks[0].source_kind is TaskSourceKind.LINKAGE
    assert all(len(task.detail or "") <= 1_000 for task in index.tasks)
    assert all(task.source_url.startswith("https://github.com/") for task in index.tasks)
    assert all(GROUNDING_COMMIT in task.source_url for task in index.tasks)


@pytest.mark.parametrize(
    ("status", "source", "limit"),
    [
        ("bad", "all", 10),
        ("open", "bad", 10),
        ("open", "all", 0),
        ("open", "all", 101),
    ],
)
def test_list_request_validation(
    index: TaskIndex,
    status: str,
    source: str,
    limit: int,
):
    with pytest.raises(TaskRequestError):
        index.list_tasks(status=status, source=source, limit=limit)


def test_github_issues_content_cannot_enter_index(index: TaskIndex):
    serialized = repr(index.tasks)
    assert "SECRET-ISSUE" not in serialized
    assert "github_issues" not in serialized


def test_task_index_construction_and_listing_make_zero_network_calls(
    task_snapshot: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    def blocked_socket(*_args, **_kwargs):
        raise AssertionError("task index attempted network I/O")

    monkeypatch.setattr(socket, "socket", blocked_socket)
    index = TaskIndex(validate_snapshot(task_snapshot, require_read_only=False))
    assert index.list_tasks(status="all", limit=100).count > 0


@pytest.mark.parametrize(
    ("path", "replacement", "message"),
    [
        (DECOMP_PATH, "wrong,headers\nx,y\n", "unexpected headers"),
        (LINKAGE_PATH, "wrong,headers\nx,y\n", "unexpected headers"),
        (
            QA_PATH,
            "# QA\n## Master report ledger (deduped ~24 unique)\n"
            "| Wrong | Header |\n|---|---|\n| a | b |\n",
            "header is missing",
        ),
        (
            CATALOG_PATH,
            "# Catalog\n## Full Missing Native Behavior Queue\n",
            "table header|sections",
        ),
    ],
)
def test_malformed_canonical_sources_fail_startup(
    task_snapshot: Path,
    path: str,
    replacement: str,
    message: str,
):
    _write(task_snapshot, path, replacement)
    _refresh_manifest(task_snapshot)
    with pytest.raises(TaskIndexError, match=message):
        TaskIndex(validate_snapshot(task_snapshot, require_read_only=False))


def test_missing_required_task_source_fails_startup(task_snapshot: Path):
    (task_snapshot / "content" / LINKAGE_PATH).unlink()
    _refresh_manifest(task_snapshot)
    with pytest.raises(TaskIndexError, match="required task sources"):
        TaskIndex(validate_snapshot(task_snapshot, require_read_only=False))


def test_real_task_grounding_counts_and_statuses():
    assert REAL_SNAPSHOT.is_dir()
    index = TaskIndex(validate_snapshot(REAL_SNAPSHOT))
    counts = Counter(task.source_kind for task in index.tasks)
    assert counts[TaskSourceKind.DECOMP] == 208
    assert counts[TaskSourceKind.LINKAGE] == 29
    assert counts[TaskSourceKind.HANDOFF] > 0
    assert counts[TaskSourceKind.QA] > 0
    assert counts[TaskSourceKind.CATALOG] > 0
    player = next(
        task
        for task in index.tasks
        if task.id == "linkage:c3dplayer:free-roam-feel"
    )
    assert player.status is TaskStatus.BLOCKED
    assert "linked-blocked" in (player.detail or "")
    wontfix = next(task for task in index.tasks if task.id == "qa:12")
    assert wontfix.status is TaskStatus.DONE
    assert "WONTFIX" in (wontfix.detail or "")
