"""Durable append-only NDJSON audit records for collaboration tools."""

from __future__ import annotations

import fcntl
import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Mapping, TypeVar

MAX_TRANSACTION_BYTES = 64 * 1024 * 1024
MAX_AUDIT_RECORD_BYTES = 64 * 1024

T = TypeVar("T")


class AuditUnavailableError(RuntimeError):
    """Raised when a call cannot be recorded durably."""


@dataclass(frozen=True)
class AuditDecision(Generic[T]):
    """One transaction result whose record is durable before value is returned."""

    record: Mapping[str, Any]
    value: T


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, record: Mapping[str, Any]) -> None:
        """Append and fsync one record while rejecting links and unsafe modes."""
        payload = (
            json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode("utf-8")
        flags = (
            os.O_APPEND
            | os.O_CREAT
            | os.O_WRONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise AuditUnavailableError("audit log cannot be opened safely") from exc
        locked = False
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise AuditUnavailableError("audit log must be a regular file")
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise AuditUnavailableError("audit log must have mode 0600")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = True
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise AuditUnavailableError("audit append did not complete")
                view = view[written:]
            os.fsync(descriptor)
        except OSError as exc:
            raise AuditUnavailableError("audit append did not complete") from exc
        finally:
            try:
                if locked:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def transact(
        self,
        decide: Callable[[tuple[Mapping[str, Any], ...]], AuditDecision[T]],
    ) -> T:
        """Read the bounded ledger and append one decision under one fsynced lock."""
        flags = (
            os.O_APPEND
            | os.O_CREAT
            | os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise AuditUnavailableError("audit log cannot be opened safely") from exc
        locked = False
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise AuditUnavailableError("audit log must be a regular file")
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise AuditUnavailableError("audit log must have mode 0600")
            if metadata.st_size > MAX_TRANSACTION_BYTES:
                raise AuditUnavailableError("audit ledger exceeds its safe bound")

            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = True
            metadata = os.fstat(descriptor)
            if metadata.st_size > MAX_TRANSACTION_BYTES:
                raise AuditUnavailableError("audit ledger exceeds its safe bound")
            raw = os.pread(descriptor, metadata.st_size, 0)
            if len(raw) != metadata.st_size:
                raise AuditUnavailableError("audit ledger read did not complete")
            if raw and not raw.endswith(b"\n"):
                raise AuditUnavailableError("audit ledger contains a partial record")

            records: list[Mapping[str, Any]] = []
            for line in raw.splitlines():
                if len(line) > MAX_AUDIT_RECORD_BYTES:
                    raise AuditUnavailableError("audit record exceeds its safe bound")
                try:
                    parsed = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise AuditUnavailableError("audit ledger is malformed") from exc
                if not isinstance(parsed, dict):
                    raise AuditUnavailableError("audit ledger record must be an object")
                records.append(parsed)

            decision = decide(tuple(records))
            payload = (
                json.dumps(decision.record, separators=(",", ":"), sort_keys=True)
                + "\n"
            ).encode("utf-8")
            if len(payload) > MAX_AUDIT_RECORD_BYTES:
                raise AuditUnavailableError("audit record exceeds its safe bound")
            if metadata.st_size + len(payload) > MAX_TRANSACTION_BYTES:
                raise AuditUnavailableError("audit ledger exceeds its safe bound")
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise AuditUnavailableError("audit append did not complete")
                view = view[written:]
            os.fsync(descriptor)
            return decision.value
        except AuditUnavailableError:
            raise
        except OSError as exc:
            raise AuditUnavailableError("audit transaction did not complete") from exc
        finally:
            try:
                if locked:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
