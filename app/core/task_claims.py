"""Audited, expiring ownership claims over committed JN Engine tasks."""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.collaboration.audit import AuditDecision, AuditLog, AuditUnavailableError
from app.collaboration.errors import (
    CollaborationError,
    bad_args,
    conflict,
    task_not_found,
    upstream_unavailable,
    write_disabled,
)
from app.core.task_index import TaskIndex, TaskRecord, TaskStatus

MIN_CLAIM_MINUTES = 15
MAX_CLAIM_MINUTES = 1_440
DEFAULT_CLAIM_MINUTES = 120

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_CLAIM_ID = re.compile(r"^[A-Za-z0-9_-]{24}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class ClaimedTask:
    task: TaskRecord
    claim_id: str
    owner: str
    claimed_at: str
    expires_at: str
    replayed: bool


@dataclass(frozen=True)
class ReleasedTask:
    task_id: str
    claim_id: str
    owner: str
    released: bool
    released_at: str


@dataclass(frozen=True)
class _ActiveClaim:
    task_id: str
    claim_id: str
    owner: str
    idempotency_key: str
    claimed_at: str
    expires_at: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 32:
        raise AuditUnavailableError("claim ledger timestamp is malformed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuditUnavailableError("claim ledger timestamp is malformed") from exc
    if parsed.tzinfo is None:
        raise AuditUnavailableError("claim ledger timestamp lacks a timezone")
    return parsed.astimezone(timezone.utc)


def _bounded_identity(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 160
        or not value.isprintable()
        or _CONTROL.search(value)
    ):
        raise bad_args("authenticated caller identity is invalid")
    return value


def _task_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or value != value.strip()
        or _CONTROL.search(value)
    ):
        raise bad_args("task_id must be an exact printable committed task ID")
    return value


def _idempotency_key(value: object) -> str:
    if not isinstance(value, str) or _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise bad_args(
            "idempotency_key must be 8..64 characters of letters, digits, "
            "dot, underscore, or hyphen"
        )
    return value


def _duration(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not MIN_CLAIM_MINUTES <= value <= MAX_CLAIM_MINUTES
    ):
        raise bad_args(
            f"duration_minutes must be in {MIN_CLAIM_MINUTES}..{MAX_CLAIM_MINUTES}"
        )
    return value


def _claim_id(value: object) -> str:
    if not isinstance(value, str) or _CLAIM_ID.fullmatch(value) is None:
        raise bad_args("claim_id is invalid; use the value returned by claim_task")
    return value


def _ledger_claims(
    records: tuple[Mapping[str, Any], ...], now: datetime
) -> dict[str, _ActiveClaim]:
    claims: dict[str, _ActiveClaim] = {}
    for record in records:
        tool = record.get("tool")
        if tool not in {"claim_task", "release_task"}:
            continue
        outcome = record.get("outcome")
        if tool == "claim_task" and outcome == "claimed":
            args = record.get("args")
            task_id = args.get("task_id") if isinstance(args, dict) else None
            key = args.get("idempotency_key") if isinstance(args, dict) else None
            owner = record.get("caller_identity")
            claim_id = record.get("claim_id")
            claimed_at = record.get("claimed_at")
            expires_at = record.get("expires_at")
            if (
                not isinstance(task_id, str)
                or not task_id
                or len(task_id) > 256
                or task_id != task_id.strip()
                or _CONTROL.search(task_id)
                or not isinstance(key, str)
                or _IDEMPOTENCY_KEY.fullmatch(key) is None
                or not isinstance(owner, str)
                or not owner
                or len(owner) > 160
                or not owner.isprintable()
                or _CONTROL.search(owner)
                or not isinstance(claim_id, str)
                or _CLAIM_ID.fullmatch(claim_id) is None
                or not isinstance(claimed_at, str)
                or not isinstance(expires_at, str)
            ):
                raise AuditUnavailableError("claim ledger record is malformed")
            start = _parse_timestamp(claimed_at)
            expiry = _parse_timestamp(expires_at)
            if expiry <= start or expiry - start > timedelta(minutes=MAX_CLAIM_MINUTES):
                raise AuditUnavailableError("claim ledger expiry is malformed")
            claims[task_id] = _ActiveClaim(
                task_id=task_id,
                claim_id=claim_id,
                owner=owner,
                idempotency_key=key,
                claimed_at=claimed_at,
                expires_at=expires_at,
            )
        elif tool == "release_task" and outcome == "released":
            args = record.get("args")
            task_id = args.get("task_id") if isinstance(args, dict) else None
            claim_id = args.get("claim_id") if isinstance(args, dict) else None
            owner = record.get("caller_identity")
            if (
                not isinstance(task_id, str)
                or not task_id
                or len(task_id) > 256
                or task_id != task_id.strip()
                or _CONTROL.search(task_id)
                or not isinstance(claim_id, str)
                or _CLAIM_ID.fullmatch(claim_id) is None
                or not isinstance(owner, str)
                or not owner
                or len(owner) > 160
                or not owner.isprintable()
                or _CONTROL.search(owner)
            ):
                raise AuditUnavailableError("release ledger record is malformed")
            current = claims.get(task_id)
            if current is None or current.claim_id != claim_id or current.owner != owner:
                raise AuditUnavailableError("release ledger does not match active ownership")
            claims.pop(task_id)
    return {
        task_id: claim
        for task_id, claim in claims.items()
        if _parse_timestamp(claim.expires_at) > now
    }


def _safe_arg(value: object, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) else None


class TaskClaimService:
    """Serialize claim decisions and their audit event in one durable append."""

    def __init__(
        self,
        tasks: TaskIndex,
        audit: AuditLog,
        *,
        clock: Callable[[], datetime] = _utc_now,
        claim_id_factory: Callable[[], str] = lambda: secrets.token_urlsafe(18),
    ) -> None:
        self._tasks = tasks
        self._audit = audit
        self._clock = clock
        self._claim_id_factory = claim_id_factory

    def claim_task(
        self,
        *,
        task_id: object,
        duration_minutes: object,
        idempotency_key: object,
        caller_identity: object,
    ) -> ClaimedTask:
        now = self._clock().astimezone(timezone.utc)
        timestamp = _timestamp(now)

        def decide(records: tuple[Mapping[str, Any], ...]):
            failure: CollaborationError | None = None
            result: ClaimedTask | None = None
            valid_task_id: str | None = None
            valid_key: str | None = None
            valid_duration: int | None = None
            valid_owner: str | None = None
            claim_id: str | None = None
            claimed_at: str | None = None
            expires_at: str | None = None
            try:
                valid_owner = _bounded_identity(caller_identity)
                valid_task_id = _task_id(task_id)
                valid_key = _idempotency_key(idempotency_key)
                valid_duration = _duration(duration_minutes)
                task = self._tasks.get_task(valid_task_id)
                if task is None:
                    raise task_not_found()
                if task.status is TaskStatus.DONE:
                    raise conflict("completed tasks cannot be claimed")
                active = _ledger_claims(records, now).get(valid_task_id)
                if active is not None:
                    if (
                        active.owner == valid_owner
                        and active.idempotency_key == valid_key
                    ):
                        claim_id = active.claim_id
                        claimed_at = active.claimed_at
                        expires_at = active.expires_at
                        result = ClaimedTask(
                            task=task,
                            claim_id=claim_id,
                            owner=valid_owner,
                            claimed_at=claimed_at,
                            expires_at=expires_at,
                            replayed=True,
                        )
                    else:
                        raise conflict(
                            f"task is claimed by {active.owner} until {active.expires_at}"
                        )
                else:
                    claim_id = self._claim_id_factory()
                    if _CLAIM_ID.fullmatch(claim_id) is None:
                        raise RuntimeError("claim ID generator returned an unsafe value")
                    claimed_at = timestamp
                    expires_at = _timestamp(now + timedelta(minutes=valid_duration))
                    result = ClaimedTask(
                        task=task,
                        claim_id=claim_id,
                        owner=valid_owner,
                        claimed_at=claimed_at,
                        expires_at=expires_at,
                        replayed=False,
                    )
            except CollaborationError as exc:
                failure = exc

            record = {
                "timestamp": timestamp,
                "caller_identity": valid_owner or _safe_arg(caller_identity, 160),
                "tool": "claim_task",
                "args": {
                    "task_id": valid_task_id or _safe_arg(task_id, 256),
                    "duration_minutes": valid_duration,
                    "idempotency_key": valid_key or _safe_arg(idempotency_key, 64),
                },
                "snapshot_commit": self._tasks.snapshot.manifest.commit,
                "claim_id": claim_id,
                "claimed_at": claimed_at,
                "expires_at": expires_at,
                "outcome": (
                    failure.code
                    if failure is not None
                    else (
                        "replayed"
                        if result is not None and result.replayed
                        else "claimed"
                    )
                ),
            }
            return AuditDecision(record=record, value=(result, failure))

        try:
            result, failure = self._audit.transact(decide)
        except AuditUnavailableError as exc:
            raise upstream_unavailable() from exc
        except Exception as exc:
            raise upstream_unavailable() from exc
        if failure is not None:
            raise failure
        if result is None:
            raise upstream_unavailable()
        return result

    def release_task(
        self,
        *,
        task_id: object,
        claim_id: object,
        caller_identity: object,
    ) -> ReleasedTask:
        now = self._clock().astimezone(timezone.utc)
        timestamp = _timestamp(now)

        def decide(records: tuple[Mapping[str, Any], ...]):
            failure: CollaborationError | None = None
            result: ReleasedTask | None = None
            valid_task_id: str | None = None
            valid_claim_id: str | None = None
            valid_owner: str | None = None
            try:
                valid_owner = _bounded_identity(caller_identity)
                valid_task_id = _task_id(task_id)
                valid_claim_id = _claim_id(claim_id)
                active = _ledger_claims(records, now).get(valid_task_id)
                if active is None:
                    result = ReleasedTask(
                        task_id=valid_task_id,
                        claim_id=valid_claim_id,
                        owner=valid_owner,
                        released=False,
                        released_at=timestamp,
                    )
                elif active.owner != valid_owner:
                    raise conflict(
                        f"task is claimed by {active.owner} until {active.expires_at}"
                    )
                elif active.claim_id != valid_claim_id:
                    raise conflict("claim_id does not match the caller's active claim")
                else:
                    result = ReleasedTask(
                        task_id=valid_task_id,
                        claim_id=valid_claim_id,
                        owner=valid_owner,
                        released=True,
                        released_at=timestamp,
                    )
            except CollaborationError as exc:
                failure = exc

            record = {
                "timestamp": timestamp,
                "caller_identity": valid_owner or _safe_arg(caller_identity, 160),
                "tool": "release_task",
                "args": {
                    "task_id": valid_task_id or _safe_arg(task_id, 256),
                    "claim_id": valid_claim_id or _safe_arg(claim_id, 24),
                },
                "snapshot_commit": self._tasks.snapshot.manifest.commit,
                "outcome": (
                    failure.code
                    if failure is not None
                    else (
                        "released"
                        if result is not None and result.released
                        else "not_claimed"
                    )
                ),
            }
            return AuditDecision(record=record, value=(result, failure))

        try:
            result, failure = self._audit.transact(decide)
        except AuditUnavailableError as exc:
            raise upstream_unavailable() from exc
        except Exception as exc:
            raise upstream_unavailable() from exc
        if failure is not None:
            raise failure
        if result is None:
            raise upstream_unavailable()
        return result


class WriteDisabledTaskClaimService:
    """Register the tools while failing closed outside the write-enabled profile."""

    def claim_task(self, **_kwargs: object) -> ClaimedTask:
        raise write_disabled()

    def release_task(self, **_kwargs: object) -> ReleasedTask:
        raise write_disabled()
