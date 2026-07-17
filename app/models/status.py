"""Structured output for the live required-context status tool."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.core.check_status import CheckStatus
from app.models.common import OutputModel


class RequiredContextOutput(OutputModel):
    state: Literal[
        "queued",
        "in_progress",
        "success",
        "failure",
        "neutral",
        "cancelled",
        "timed_out",
        "missing",
    ]
    run_id: int | None = Field(default=None, ge=1)
    run_url: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


class RequiredContexts(OutputModel):
    core: RequiredContextOutput
    assets: RequiredContextOutput


class StatusArtifactOutput(OutputModel):
    name: str
    size_bytes: int = Field(ge=0)
    download_url: str
    expired: bool


class CheckStatusOutput(OutputModel):
    repository: Literal["alexscott2718-gif/jn-engine"]
    ref: str
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    required_contexts: RequiredContexts
    overall: Literal[
        "success",
        "failure",
        "neutral",
        "cancelled",
        "timed_out",
        "in_progress",
        "queued",
        "blocked",
    ]
    blocked_reason: str | None = None
    artifacts: list[StatusArtifactOutput]
    checked_at: str


def check_status_output(status: CheckStatus) -> CheckStatusOutput:
    contexts = status.required_contexts
    return CheckStatusOutput(
        repository=status.repository,
        ref=status.ref,
        commit=status.commit,
        required_contexts=RequiredContexts(
            core=RequiredContextOutput(**vars(contexts["core"])),
            assets=RequiredContextOutput(**vars(contexts["assets"])),
        ),
        overall=status.overall,
        blocked_reason=status.blocked_reason,
        artifacts=[StatusArtifactOutput(**vars(item)) for item in status.artifacts],
        checked_at=status.checked_at,
    )
