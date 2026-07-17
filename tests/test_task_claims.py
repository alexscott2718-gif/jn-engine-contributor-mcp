"""Hermetic ownership, expiry, idempotency, concurrency, and audit tests."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.collaboration.audit import AuditLog
from app.collaboration.errors import CollaborationError
from app.core.task_claims import TaskClaimService, WriteDisabledTaskClaimService
from app.core.task_index import TaskRecord, TaskSourceKind, TaskStatus

COMMIT = "9" * 40
NOW = datetime(2026, 7, 17, 16, 0, tzinfo=timezone.utc)
TASK_ID = "handoff:claim-release-task"
CLAIM_ID = "A" * 24


def _record(task_id: str = TASK_ID, status: TaskStatus = TaskStatus.OPEN) -> TaskRecord:
    return TaskRecord(
        id=task_id,
        title="Add task ownership",
        status=status,
        source_kind=TaskSourceKind.HANDOFF,
        category="gateway",
        detail="Auditable ownership and expiry.",
        source_path="docs/decomp/_next_session.md",
        source_line=10,
        source_url="https://example.invalid/task",
        commit=COMMIT,
    )


class _Tasks:
    def __init__(self) -> None:
        self.snapshot = SimpleNamespace(manifest=SimpleNamespace(commit=COMMIT))
        self.records = {
            TASK_ID: _record(),
            "handoff:done": _record("handoff:done", TaskStatus.DONE),
        }

    def get_task(self, task_id: str):
        return self.records.get(task_id)


def _service(
    tmp_path: Path,
    *,
    clock=lambda: NOW,
    claim_id: str = CLAIM_ID,
) -> tuple[TaskClaimService, Path]:
    audit_path = tmp_path / "tool_calls.ndjson"
    return (
        TaskClaimService(
            _Tasks(),
            AuditLog(audit_path),
            clock=clock,
            claim_id_factory=lambda: claim_id,
        ),
        audit_path,
    )


def _claim(service: TaskClaimService, **overrides):
    args = {
        "task_id": TASK_ID,
        "duration_minutes": 120,
        "idempotency_key": "session-claim-001",
        "caller_identity": "github:alice",
    }
    args.update(overrides)
    return service.claim_task(**args)


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_claim_is_owned_expiring_idempotent_and_fsynced_to_audit(tmp_path: Path):
    service, audit_path = _service(tmp_path)
    first = _claim(service)
    replay = _claim(service)

    assert first.claim_id == CLAIM_ID
    assert first.owner == "github:alice"
    assert first.claimed_at == "2026-07-17T16:00:00Z"
    assert first.expires_at == "2026-07-17T18:00:00Z"
    assert first.replayed is False
    assert replay == type(replay)(**{**first.__dict__, "replayed": True})
    assert audit_path.stat().st_mode & 0o777 == 0o600
    records = _records(audit_path)
    assert [record["outcome"] for record in records] == ["claimed", "replayed"]
    assert records[0]["caller_identity"] == "github:alice"
    assert records[0]["snapshot_commit"] == COMMIT


def test_active_claim_rejects_other_owner_and_same_owner_new_work(tmp_path: Path):
    service, audit_path = _service(tmp_path)
    _claim(service)
    for owner, key in (
        ("github:bob", "session-claim-002"),
        ("github:alice", "session-claim-003"),
    ):
        with pytest.raises(CollaborationError) as raised:
            _claim(service, caller_identity=owner, idempotency_key=key)
        assert raised.value.code == "conflict"
        assert "github:alice" in raised.value.detail
        assert "2026-07-17T18:00:00Z" in raised.value.detail
    assert [record["outcome"] for record in _records(audit_path)] == [
        "claimed",
        "conflict",
        "conflict",
    ]


def test_expiry_allows_a_new_owner_without_a_release(tmp_path: Path):
    current = [NOW]
    service, _ = _service(tmp_path, clock=lambda: current[0])
    _claim(service, duration_minutes=15)
    current[0] = NOW + timedelta(minutes=15)
    claimed = _claim(
        service,
        caller_identity="github:bob",
        idempotency_key="session-claim-002",
    )
    assert claimed.owner == "github:bob"
    assert claimed.replayed is False


def test_release_requires_owner_and_claim_id_then_replays_safely(tmp_path: Path):
    service, audit_path = _service(tmp_path)
    claimed = _claim(service)

    with pytest.raises(CollaborationError) as raised:
        service.release_task(
            task_id=TASK_ID,
            claim_id=claimed.claim_id,
            caller_identity="github:bob",
        )
    assert raised.value.code == "conflict"
    with pytest.raises(CollaborationError) as raised:
        service.release_task(
            task_id=TASK_ID,
            claim_id="B" * 24,
            caller_identity="github:alice",
        )
    assert raised.value.code == "conflict"

    released = service.release_task(
        task_id=TASK_ID,
        claim_id=claimed.claim_id,
        caller_identity="github:alice",
    )
    replay = service.release_task(
        task_id=TASK_ID,
        claim_id=claimed.claim_id,
        caller_identity="github:alice",
    )
    assert released.released is True
    assert replay.released is False
    assert [record["outcome"] for record in _records(audit_path)] == [
        "claimed",
        "conflict",
        "conflict",
        "released",
        "not_claimed",
    ]


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"task_id": "handoff:missing"}, "not_found"),
        ({"task_id": "handoff:done"}, "conflict"),
        ({"task_id": " bad "}, "bad_args"),
        ({"duration_minutes": 14}, "bad_args"),
        ({"duration_minutes": True}, "bad_args"),
        ({"idempotency_key": "short"}, "bad_args"),
    ],
)
def test_invalid_claims_fail_before_state_change_and_are_audited(
    tmp_path: Path, overrides: dict, code: str
):
    service, audit_path = _service(tmp_path)
    with pytest.raises(CollaborationError) as raised:
        _claim(service, **overrides)
    assert raised.value.code == code
    assert _records(audit_path)[-1]["outcome"] == code


def test_malformed_or_unsafe_ledger_fails_closed(tmp_path: Path):
    service, audit_path = _service(tmp_path)
    audit_path.write_text('{"tool":"claim_task","outcome":"claimed"}\n')
    audit_path.chmod(0o600)
    with pytest.raises(CollaborationError) as raised:
        _claim(service)
    assert raised.value.code == "upstream_unavailable"


def test_partial_or_linked_ledger_fails_closed(tmp_path: Path):
    service, audit_path = _service(tmp_path)
    audit_path.write_text('{"tool":"claim_task"}')
    audit_path.chmod(0o600)
    with pytest.raises(CollaborationError) as raised:
        _claim(service)
    assert raised.value.code == "upstream_unavailable"

    audit_path.unlink()
    target = tmp_path / "other.ndjson"
    target.write_text("")
    target.chmod(0o600)
    audit_path.symlink_to(target)
    with pytest.raises(CollaborationError) as raised:
        _claim(service)
    assert raised.value.code == "upstream_unavailable"

    audit_path.chmod(0o644)
    with pytest.raises(CollaborationError) as raised:
        _claim(service)
    assert raised.value.code == "upstream_unavailable"


def test_concurrent_callers_cannot_both_acquire_one_task(tmp_path: Path):
    service, audit_path = _service(tmp_path)

    def attempt(owner: str, key: str) -> str:
        try:
            _claim(service, caller_identity=owner, idempotency_key=key)
            return "claimed"
        except CollaborationError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda pair: attempt(*pair),
                [
                    ("github:alice", "session-claim-001"),
                    ("github:bob", "session-claim-002"),
                ],
            )
        )
    assert sorted(outcomes) == ["claimed", "conflict"]
    assert sorted(record["outcome"] for record in _records(audit_path)) == [
        "claimed",
        "conflict",
    ]


def test_write_disabled_service_registers_but_rejects_both_tools():
    service = WriteDisabledTaskClaimService()
    for method in (service.claim_task, service.release_task):
        with pytest.raises(CollaborationError) as raised:
            method()
        assert raised.value.code == "write_disabled"
