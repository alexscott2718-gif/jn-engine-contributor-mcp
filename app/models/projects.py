"""Frozen project-context response models."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.core.project_context import ProjectContext
from app.core.snapshot import Snapshot
from app.models.common import (
    OutputModel,
    SnapshotRef,
    SourceRef,
    snapshot_ref,
    source_ref,
)
from app.models.tasks import Task, task_output


class ImportantFile(OutputModel):
    title: str
    role: str
    source: SourceRef


class ProjectContextOutput(OutputModel):
    project: Literal["jn-engine"] = "jn-engine"
    snapshot: SnapshotRef
    summary: str = Field(max_length=500)
    current_state: list[str]
    important_files: list[ImportantFile] = Field(max_length=8)
    open_tasks: list[Task] = Field(max_length=10)
    context: str = Field(max_length=20_000)


def project_context_output(
    result: ProjectContext,
    snapshot: Snapshot,
) -> ProjectContextOutput:
    return ProjectContextOutput(
        snapshot=snapshot_ref(snapshot),
        summary=result.summary,
        current_state=list(result.current_state),
        important_files=[
            ImportantFile(
                title=item.title,
                role=item.role,
                source=source_ref(path=item.path, line=item.line, url=item.url),
            )
            for item in result.important_files
        ],
        open_tasks=[task_output(task) for task in result.open_tasks],
        context=result.context,
    )
