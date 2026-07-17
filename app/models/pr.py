"""Structured output for the live PR-only write tool."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.core.open_pr import OpenedPr
from app.models.common import OutputModel


class OpenPrOutput(OutputModel):
    repository: Literal["alexscott2718-gif/jn-engine"]
    base_ref: Literal["refs/heads/master"]
    base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    branch: str
    head_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    pr_number: int = Field(ge=1)
    pr_url: str
    created_branch: bool
    replayed: bool
    opened_at: str


def open_pr_output(result: OpenedPr) -> OpenPrOutput:
    return OpenPrOutput(
        repository=result.repository,
        base_ref=result.base_ref,
        base_commit=result.base_commit,
        branch=result.branch,
        head_commit=result.head_commit,
        pr_number=result.pr_number,
        pr_url=result.pr_url,
        created_branch=result.created_branch,
        replayed=result.replayed,
        opened_at=result.opened_at,
    )
