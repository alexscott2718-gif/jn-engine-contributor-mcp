"""Closed-source, deterministic, bounded contributor-context assembly."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

import app.core.project_context as context_module
from app.core.project_context import (
    CONTEXT_PATHS,
    ProjectContextAssembler,
    ProjectContextError,
    ProjectContextRequestError,
)
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
    TaskStatus,
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
def context_snapshot(snapshot: Path) -> Path:
    handoff_tasks = "\n".join(f"- ({index}) Open task {index}" for index in range(12))
    _write(
        snapshot,
        "AGENTS.md",
        """# JN Engine agent instructions
## Mission (current)
Native Linux port. Branch: `master`. The committed engine is the product.
## Shared memory
Commit durable project facts.
## The current frontier
Recover grounded runtime behavior.
## Gotchas
Measure before changing invariants.
""",
    )
    _write(
        snapshot,
        "README.md",
        """# jn-engine

A clean-room engine reconstruction grounded by Direct3D 7 capture evidence.

## Build
Run the committed Makefile targets.
## Repo layout
Source, instrumentation, tools, and docs are separate roots.
""",
    )
    _write(
        snapshot,
        "docs/ARCHITECTURE.md",
        """# Architecture
## 1. The 10,000-foot view
The engine has native, replay, and capture modes.
## 9. Build & run
Use fixed Make targets.
## 12. The invariants you must not break
Captured matrices and command order are measured facts.
""",
    )
    _write(
        snapshot,
        "docs/PROJECT_HISTORY.md",
        """# Project history
## Earlier checkpoint (2026-06-01)
Old state that must not be reported as latest.
## Invariants (do not relitigate)
Preserve measured renderer behavior.
## Where to go next
Use the current handoff.
## Latest grounded checkpoint (2026-07-04)
The Windows build was validated.
""",
    )
    _write(
        snapshot,
        HANDOFF_PATH,
        f"""# Next session
## Current state (branch: `native-port`)
The handoff branch label is stale.
## Your task this session: live options
{handoff_tasks}
- [x] Completed task
## Hard rules
Do not infer behavior.
## Definition of done for this session
Record measured verification.
""",
    )
    _write(
        snapshot,
        "docs/native_port_plan.md",
        """# Native port plan
## 0. Where the engine stands
The base engine runs.
## 1. The implementation contract
Port only grounded behavior.
## 3. Validation (every wave)
Compare against captured evidence.
""",
    )
    _write(
        snapshot,
        "docs/local_env.md",
        """# Local environment
## Required variables
Read values from the local environment.
## Recommended setup
Keep secrets outside the repository.
## Rebuilding the proxy after changing the receiver
Rebuild before deployment.
""",
    )
    _write(
        snapshot,
        "docs/claude_code_failure_patterns.md",
        """# Failure patterns
## Recurring failure patterns
### 1. Repeating known conclusions
Trust committed handoffs.
### 2. Fixing the wrong layer
Check the final predicate.
### 3. Making proxy assumptions
Measure runtime calls.
### 4. Trusting deployed binaries
Verify deployed hashes.
### 5. Launching the game
Use the visible Windows session.
### 9. Blocking the game render thread
Keep capture bounded.
### 10. Misreading alpha
Separate clear and draw evidence.
### 11. Continuing a known dead-end
Stop when the handoff closes a target.
""",
    )
    _write(
        snapshot,
        QA_PATH,
        """# QA
## Master report ledger (deduped)
| # | Level | Model | Cat | Issue | Group | Status |
|---|---|---|---|---|---|---|
| 1 | l1 | A | GFX | open issue | A | NOT FIXED |
""",
    )
    _write(
        snapshot,
        LINKAGE_PATH,
        ",".join(LINKAGE_HEADERS)
        + "\nCAlpha,aspect,domain,linked-blocked,,docs/decomp/CAlpha.md,open\n",
    )
    _write(
        snapshot,
        DECOMP_PATH,
        ",".join(DECOMP_HEADERS)
        + "\nCAlpha,CBase,00400000,,1,family,1,spec,owner,High,done\n",
    )
    _write(
        snapshot,
        CATALOG_PATH,
        """# Catalog
## Full Missing Native Behavior Queue
| Rank | Score | FourCC | Class | Family | Inst | Lvls | Visual | Levels |
|---|---|---|---|---|---|---|---|---|
| 1 | 10 | 3AAA | CAlpha | actor | 1 | 1 | missing | l1 |
## Enemy-Family Specs With No Current `.gam` Placement
| FourCC | Class | Visual status | Decomp doc |
|---|---|---|---|
| 3BBB | CBeta | unused | docs/decomp/CBeta.md |
""",
    )
    _write(snapshot, "docs/private_notes.md", "PRIVATE-MARKER must never appear\n")
    _refresh_manifest(snapshot)
    return snapshot


@pytest.fixture()
def assembler(context_snapshot: Path) -> ProjectContextAssembler:
    loaded = validate_snapshot(context_snapshot, require_read_only=False)
    return ProjectContextAssembler(loaded, TaskIndex(loaded))


def test_summary_current_state_stale_notice_and_latest_history(
    assembler: ProjectContextAssembler,
):
    result = assembler.build()
    assert result.summary == (
        "A clean-room engine reconstruction grounded by Direct3D 7 capture evidence."
    )
    state = "\n".join(result.current_state)
    assert "native-port" in state
    assert "Stale branch notice" in state
    assert "refs/heads/master" in state
    assert GROUNDING_COMMIT in state
    assert "2026-07-04" in state
    assert "2026-06-01" not in state
    assert "linked-blocked" in state


def test_exact_eight_important_files_with_commit_pinned_sources(
    assembler: ProjectContextAssembler,
):
    files = assembler.build().important_files
    assert len(files) == 8
    assert tuple(file.path for file in files) == CONTEXT_PATHS
    assert all(file.line == 1 for file in files)
    assert all(GROUNDING_COMMIT in file.url for file in files)
    assert all(file.url.startswith("https://github.com/") for file in files)


def test_reads_only_the_closed_eight_file_set(
    context_snapshot: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    loaded = validate_snapshot(context_snapshot, require_read_only=False)
    tasks = TaskIndex(loaded)
    seen: list[str] = []
    original = context_module.resolve_relative_file

    def recording_resolver(snapshot, path):
        seen.append(path)
        return original(snapshot, path)

    monkeypatch.setattr(context_module, "resolve_relative_file", recording_resolver)
    ProjectContextAssembler(loaded, tasks)
    assert tuple(seen) == CONTEXT_PATHS


def test_open_tasks_are_bounded_and_grounded(assembler: ProjectContextAssembler):
    tasks = assembler.build().open_tasks
    assert len(tasks) == 10
    assert all(task.status in {TaskStatus.OPEN, TaskStatus.BLOCKED} for task in tasks)
    assert all(task.commit == GROUNDING_COMMIT for task in tasks)


@pytest.mark.parametrize("max_chars", [1_000, 12_000, 20_000])
def test_context_bounds_determinism_and_allowlist(
    assembler: ProjectContextAssembler,
    max_chars: int,
):
    first = assembler.build(max_chars=max_chars)
    second = assembler.build(max_chars=max_chars)
    assert first == second
    assert len(first.context) <= max_chars
    assert "Stale branch notice" in first.context
    assert "PRIVATE-MARKER" not in first.context
    assert "/home/" not in first.context


@pytest.mark.parametrize("max_chars", [0, 999, 20_001])
def test_invalid_context_bounds_fail_closed(
    assembler: ProjectContextAssembler,
    max_chars: int,
):
    with pytest.raises(ProjectContextRequestError):
        assembler.build(max_chars=max_chars)


def test_named_excerpts_cover_build_validation_invariants_and_failures(
    assembler: ProjectContextAssembler,
):
    context = assembler.build(max_chars=20_000).context
    assert "## Build" in context
    assert "## 3. Validation" in context
    assert "## 12. The invariants" in context
    assert "### 10. Misreading alpha" in context
    assert "Source: docs/local_env.md" in context


def test_current_handoff_heading_schema_is_selected(context_snapshot: Path):
    _write(
        context_snapshot,
        HANDOFF_PATH,
        """# Next Session — Portable Collaboration Handoff
## What just landed
The portable collaboration campaign is complete on master.
## Recommended next campaign: gateway collaboration tools
1. `open_pr`: create a contributor pull request.
2. `check_status`: report required checks.
## Standard validation
Run the proportional repository gates.
## Definition of done for the next session
Land one focused pull request.
""",
    )
    _refresh_manifest(context_snapshot)
    loaded = validate_snapshot(context_snapshot, require_read_only=False)
    result = ProjectContextAssembler(loaded, TaskIndex(loaded)).build(max_chars=20_000)
    assert "## What just landed" in result.context
    assert "## Recommended next campaign" in result.context
    assert "## Standard validation" in result.context
    assert [task.title for task in result.open_tasks[:2]] == [
        "open_pr: create a contributor pull request.",
        "check_status: report required checks.",
    ]


def test_master_handoff_has_no_notice_and_other_branch_does(
    context_snapshot: Path,
):
    path = context_snapshot / "content" / HANDOFF_PATH
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace("native-port", "master"), encoding="utf-8")
    _refresh_manifest(context_snapshot)
    loaded = validate_snapshot(context_snapshot, require_read_only=False)
    state = "\n".join(ProjectContextAssembler(loaded, TaskIndex(loaded)).build().current_state)
    assert "Stale branch notice" not in state

    path.write_text(original.replace("native-port", "feature/camera"), encoding="utf-8")
    _refresh_manifest(context_snapshot)
    loaded = validate_snapshot(context_snapshot, require_read_only=False)
    state = "\n".join(ProjectContextAssembler(loaded, TaskIndex(loaded)).build().current_state)
    assert "feature/camera" in state
    assert "Stale branch notice" in state


def test_missing_context_file_fails_startup(context_snapshot: Path):
    local_env = context_snapshot / "content/docs/local_env.md"
    local_env.unlink()
    _refresh_manifest(context_snapshot)
    loaded = validate_snapshot(context_snapshot, require_read_only=False)
    with pytest.raises(ProjectContextError, match="required project-context files"):
        ProjectContextAssembler(loaded, TaskIndex(loaded))


def test_missing_named_heading_fails_startup(context_snapshot: Path):
    architecture = context_snapshot / "content/docs/ARCHITECTURE.md"
    architecture.write_text(
        architecture.read_text(encoding="utf-8").replace(
            "## 12. The invariants you must not break",
            "## Removed invariant heading",
        ),
        encoding="utf-8",
    )
    _refresh_manifest(context_snapshot)
    loaded = validate_snapshot(context_snapshot, require_read_only=False)
    with pytest.raises(ProjectContextError, match="missing a named heading"):
        ProjectContextAssembler(loaded, TaskIndex(loaded))


def test_snapshot_mismatch_fails_startup(context_snapshot: Path):
    first = validate_snapshot(context_snapshot, require_read_only=False)
    second = replace(
        first,
        manifest=first.manifest.model_copy(update={"commit": "2" * 40}),
    )
    with pytest.raises(ProjectContextError, match="different snapshots"):
        ProjectContextAssembler(first, TaskIndex(second))


def test_real_project_context_grounding():
    assert REAL_SNAPSHOT.is_dir()
    loaded = validate_snapshot(REAL_SNAPSHOT)
    result = ProjectContextAssembler(loaded, TaskIndex(loaded)).build()
    assert "Direct3D 7" in result.summary
    assert len(result.important_files) == 8
    assert len(result.open_tasks) == 10
    assert len(result.context) <= 12_000
    state = "\n".join(result.current_state)
    assert "native-port" in state
    assert "refs/heads/master" in state
    assert "2026-07-04" in state
