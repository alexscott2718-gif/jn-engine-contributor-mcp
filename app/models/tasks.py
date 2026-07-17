"""Frozen committed-task response models."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.core.snapshot import Snapshot
from app.core.task_index import TaskList, TaskRecord
from app.models.common import (
    OutputModel,
    SnapshotRef,
    SourceRef,
    snapshot_ref,
    source_ref,
)

TaskStatus = Literal["open", "blocked", "done"]
TaskStatusFilter = Literal["open", "blocked", "done", "all"]
TaskSource = Literal["handoff", "qa", "linkage", "decomp", "catalog"]
TaskSourceFilter = Literal["all", "handoff", "qa", "linkage", "decomp", "catalog"]


class Task(OutputModel):
    id: str
    title: str
    status: TaskStatus
    source_kind: TaskSource
    category: str | None
    detail: str | None = Field(default=None, max_length=1_000)
    source: SourceRef
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")


class TaskListOutput(OutputModel):
    status: TaskStatusFilter
    source: TaskSourceFilter
    snapshot: SnapshotRef
    count: int = Field(ge=0, le=100)
    tasks: list[Task] = Field(max_length=100)


def task_output(record: TaskRecord) -> Task:
    return Task(
        id=record.id,
        title=record.title,
        status=record.status.value,
        source_kind=record.source_kind.value,
        category=record.category,
        detail=record.detail,
        source=source_ref(
            path=record.source_path,
            line=record.source_line,
            url=record.source_url,
        ),
        commit=record.commit,
    )


def task_list_output(task_list: TaskList, snapshot: Snapshot) -> TaskListOutput:
    return TaskListOutput(
        status=task_list.status.value,
        source=task_list.source.value,
        snapshot=snapshot_ref(snapshot),
        count=task_list.count,
        tasks=[task_output(record) for record in task_list.tasks],
    )
