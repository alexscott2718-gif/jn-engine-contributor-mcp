"""Durable append-only NDJSON audit records for collaboration tools."""

from __future__ import annotations

import fcntl
import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping


class AuditUnavailableError(RuntimeError):
    """Raised when a call cannot be recorded durably."""


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
