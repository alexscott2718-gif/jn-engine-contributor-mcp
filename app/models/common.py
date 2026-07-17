"""Shared REST and MCP output types."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config import EXPECTED_REF, EXPECTED_REPOSITORY
from app.core.snapshot import Snapshot


class OutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SnapshotRef(OutputModel):
    repository: str
    ref: str
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")


class SourceRef(OutputModel):
    path: str
    line: int | None = Field(default=None, ge=1)
    url: str


class ErrorResponse(OutputModel):
    code: str
    detail: str
    request_id: str


class HealthResponse(OutputModel):
    status: Literal["ok"] = "ok"
    app: Literal["jn-engine-contributor-mcp"] = "jn-engine-contributor-mcp"
    mode: Literal["read_only"] = "read_only"
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")


def snapshot_ref(snapshot: Snapshot) -> SnapshotRef:
    return SnapshotRef(
        repository=EXPECTED_REPOSITORY,
        ref=EXPECTED_REF,
        commit=snapshot.manifest.commit,
    )


def source_ref(*, path: str, line: int | None, url: str) -> SourceRef:
    return SourceRef(path=path, line=line, url=url)
