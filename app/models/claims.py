"""Structured outputs for expiring committed-task ownership."""

from __future__ import annotations

from pydantic import Field

from app.core.task_claims import ClaimedTask, ReleasedTask
from app.models.common import OutputModel
from app.models.tasks import Task, task_output


class ClaimTaskOutput(OutputModel):
    task: Task
    claim_id: str = Field(pattern=r"^[A-Za-z0-9_-]{24}$")
    owner: str = Field(min_length=1, max_length=160)
    claimed_at: str = Field(min_length=20, max_length=32)
    expires_at: str = Field(min_length=20, max_length=32)
    replayed: bool


class ReleaseTaskOutput(OutputModel):
    task_id: str
    claim_id: str = Field(pattern=r"^[A-Za-z0-9_-]{24}$")
    owner: str = Field(min_length=1, max_length=160)
    released: bool
    released_at: str = Field(min_length=20, max_length=32)


def claim_task_output(result: ClaimedTask) -> ClaimTaskOutput:
    return ClaimTaskOutput(
        task=task_output(result.task),
        claim_id=result.claim_id,
        owner=result.owner,
        claimed_at=result.claimed_at,
        expires_at=result.expires_at,
        replayed=result.replayed,
    )


def release_task_output(result: ReleasedTask) -> ReleaseTaskOutput:
    return ReleaseTaskOutput(
        task_id=result.task_id,
        claim_id=result.claim_id,
        owner=result.owner,
        released=result.released,
        released_at=result.released_at,
    )
