"""Protected committed-task REST adapter."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.core.task_index import TaskIndex, TaskRequestError
from app.models.tasks import TaskListOutput, task_list_output
from app.rest.errors import GatewayHTTPError

ContributorDependency = Callable[..., object]
TaskStatus = Literal["open", "blocked", "done", "all"]
TaskSource = Literal["all", "handoff", "qa", "linkage", "decomp", "catalog"]


def create_tasks_router(
    tasks: TaskIndex,
    require_contributor: ContributorDependency,
) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_contributor)])

    @router.get("/v1/tasks/list", response_model=TaskListOutput)
    def list_tasks(
        status: TaskStatus = "open",
        source: TaskSource = "all",
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> TaskListOutput:
        try:
            result = tasks.list_tasks(status=status, source=source, limit=limit)
            return task_list_output(result, tasks.snapshot)
        except TaskRequestError as exc:
            raise GatewayHTTPError(
                400,
                "invalid_request",
                "task parameters are invalid",
            ) from exc

    return router
