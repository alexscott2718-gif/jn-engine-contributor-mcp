"""Nine authenticated MCP tools over one immutable JN Engine snapshot."""

from __future__ import annotations

from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AuthProvider
from fastmcp.server.dependencies import get_access_token
from pydantic import Field

from app.config import Settings
from app.collaboration.errors import CollaborationError, tool_error
from app.core.check_status import (
    CheckStatusService,
    CredentialUnavailableStatusService,
)
from app.core.open_pr import OpenPrService, WriteDisabledOpenPrService
from app.core.task_claims import (
    DEFAULT_CLAIM_MINUTES,
    TaskClaimService,
    WriteDisabledTaskClaimService,
)
from app.core.content_search import (
    ContentSearch,
    SearchEngineError,
    SearchRequestError,
)
from app.core.path_safety import (
    ContentNotFoundError,
    ContentUnavailableError,
    InvalidContentIdError,
    StaleContentIdError,
    UnsafePathError,
)
from app.core.project_context import (
    ProjectContextAssembler,
    ProjectContextRequestError,
)
from app.core.symbol_index import SymbolIndex, SymbolRequestError
from app.core.task_index import TaskIndex, TaskRequestError
from app.models.content import (
    FetchOutput,
    SearchToolOutput,
    fetch_output,
    search_tool_output,
)
from app.models.claims import (
    ClaimTaskOutput,
    ReleaseTaskOutput,
    claim_task_output,
    release_task_output,
)
from app.models.projects import ProjectContextOutput, project_context_output
from app.models.symbols import SymbolLookupOutput, symbol_lookup_output
from app.models.pr import OpenPrOutput, open_pr_output
from app.models.status import CheckStatusOutput, check_status_output
from app.models.tasks import TaskListOutput, task_list_output

SearchScope = Literal["all", "source", "docs", "re", "tasks"]
TaskStatus = Literal["open", "blocked", "done", "all"]
TaskSource = Literal["all", "handoff", "qa", "linkage", "decomp", "catalog"]

READ_ONLY_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

LIVE_READ_ONLY_ANNOTATIONS = {
    **READ_ONLY_ANNOTATIONS,
    "openWorldHint": True,
}

PR_WRITE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}

TASK_WRITE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


def _caller_identity() -> str:
    """Extract a bounded identity claim; never use or log the bearer itself."""
    token = get_access_token()
    if token is None:
        return "local:unauthenticated"
    claims = token.claims or {}
    login = claims.get("login")
    subject = claims.get("sub")
    if isinstance(login, str) and login.isprintable() and len(login) <= 100:
        return f"github:{login}"
    if isinstance(subject, str) and subject.isprintable() and len(subject) <= 160:
        return subject
    return f"client:{token.client_id[:100]}"


def create_mcp_server(
    settings: Settings,
    *,
    auth: AuthProvider | None,
    content: ContentSearch,
    tasks: TaskIndex,
    projects: ProjectContextAssembler,
    symbols: SymbolIndex,
    statuses: CheckStatusService | CredentialUnavailableStatusService,
    pr_writes: OpenPrService | WriteDisabledOpenPrService,
    task_claims: TaskClaimService | WriteDisabledTaskClaimService,
) -> FastMCP:
    server = FastMCP(
        settings.app_name,
        version="0.1.0",
        instructions=(
            "Access the immutable JN Engine source, design, task, and reverse-engineering "
            "snapshot. Search first, claim a task before work, then release it when done."
        ),
        auth=auth,
        mask_error_details=True,
    )

    @server.tool(
        description=(
            "Search the active JN Engine snapshot by case-insensitive literal. "
            "Use this first to find source, documentation, task, or RE records; "
            "results provide commit-bound IDs for fetch."
        ),
        output_schema=SearchToolOutput.model_json_schema(),
        annotations=READ_ONLY_ANNOTATIONS,
    )
    def search(
        query: Annotated[str, Field(min_length=1, max_length=200)],
        scope: SearchScope = "all",
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> SearchToolOutput:
        try:
            return search_tool_output(
                content.search(query, scope=scope, limit=limit)
            )
        except SearchRequestError as exc:
            raise ToolError("invalid search request") from exc
        except SearchEngineError as exc:
            raise ToolError("content search is temporarily unavailable") from exc

    @server.tool(
        description=(
            "Fetch bounded text and exact metadata for one commit-bound ID returned "
            "by search. Search again when an ID belongs to an older snapshot."
        ),
        output_schema=FetchOutput.model_json_schema(),
        annotations=READ_ONLY_ANNOTATIONS,
    )
    def fetch(
        id: Annotated[str, Field(min_length=1, max_length=8_192)],
    ) -> FetchOutput:
        try:
            return fetch_output(content.fetch(id))
        except (InvalidContentIdError, UnsafePathError) as exc:
            raise ToolError("invalid content ID; use an ID returned by search") from exc
        except StaleContentIdError as exc:
            raise ToolError("snapshot changed; search again") from exc
        except ContentNotFoundError as exc:
            raise ToolError("content not found; search again") from exc
        except ContentUnavailableError as exc:
            raise ToolError("content is temporarily unavailable") from exc

    @server.tool(
        description=(
            "List deterministic tasks from the committed handoff, QA, linkage, "
            "decomp, and catalog sources. This never queries GitHub Issues."
        ),
        output_schema=TaskListOutput.model_json_schema(),
        annotations=READ_ONLY_ANNOTATIONS,
    )
    def list_tasks(
        status: TaskStatus = "open",
        source: TaskSource = "all",
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> TaskListOutput:
        try:
            result = tasks.list_tasks(status=status, source=source, limit=limit)
            return task_list_output(result, tasks.snapshot)
        except TaskRequestError as exc:
            raise ToolError("invalid task request") from exc

    @server.tool(
        description=(
            "Claim one exact committed open or blocked task for the authenticated "
            "caller. Ownership expires automatically; an idempotency_key makes "
            "retries return the same claim."
        ),
        output_schema=ClaimTaskOutput.model_json_schema(),
        annotations=TASK_WRITE_ANNOTATIONS,
    )
    def claim_task(
        task_id: Annotated[str, Field(min_length=1, max_length=256)],
        idempotency_key: Annotated[str, Field(min_length=8, max_length=64)],
        duration_minutes: Annotated[
            int,
            Field(ge=15, le=1_440),
        ] = DEFAULT_CLAIM_MINUTES,
    ) -> ClaimTaskOutput:
        try:
            return claim_task_output(
                task_claims.claim_task(
                    task_id=task_id,
                    duration_minutes=duration_minutes,
                    idempotency_key=idempotency_key,
                    caller_identity=_caller_identity(),
                )
            )
        except CollaborationError as exc:
            raise tool_error(exc) from exc

    @server.tool(
        description=(
            "Release one claim owned by the authenticated caller. The claim_id "
            "prevents a delayed retry from releasing a newer claim."
        ),
        output_schema=ReleaseTaskOutput.model_json_schema(),
        annotations=TASK_WRITE_ANNOTATIONS,
    )
    def release_task(
        task_id: Annotated[str, Field(min_length=1, max_length=256)],
        claim_id: Annotated[str, Field(min_length=24, max_length=24)],
    ) -> ReleaseTaskOutput:
        try:
            return release_task_output(
                task_claims.release_task(
                    task_id=task_id,
                    claim_id=claim_id,
                    caller_identity=_caller_identity(),
                )
            )
        except CollaborationError as exc:
            raise tool_error(exc) from exc

    @server.tool(
        description=(
            "Assemble the bounded JN Engine contributor briefing from the eight "
            "frozen project files and committed open or blocked tasks."
        ),
        output_schema=ProjectContextOutput.model_json_schema(),
        annotations=READ_ONLY_ANNOTATIONS,
    )
    def project_context(
        max_chars: Annotated[int, Field(ge=1_000, le=20_000)] = 12_000,
    ) -> ProjectContextOutput:
        try:
            result = projects.build(max_chars=max_chars)
            return project_context_output(result, projects.snapshot)
        except ProjectContextRequestError as exc:
            raise ToolError("invalid project context request") from exc

    @server.tool(
        description=(
            "Look up grounded JN Engine classes, functions, addresses, FourCCs, "
            "and linkage certificates from the committed RE indexes."
        ),
        output_schema=SymbolLookupOutput.model_json_schema(),
        annotations=READ_ONLY_ANNOTATIONS,
    )
    def lookup_symbol(
        name: Annotated[str | None, Field(min_length=1, max_length=128)] = None,
        address: Annotated[str | None, Field(min_length=1, max_length=32)] = None,
        class_name: Annotated[
            str | None,
            Field(min_length=1, max_length=128),
        ] = None,
        fourcc: Annotated[str | None, Field(min_length=4, max_length=4)] = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> SymbolLookupOutput:
        try:
            result = symbols.lookup(
                name=name,
                address=address,
                class_name=class_name,
                fourcc=fourcc,
                limit=limit,
            )
            return symbol_lookup_output(result, symbols.snapshot)
        except SymbolRequestError as exc:
            raise ToolError("invalid symbol request") from exc

    @server.tool(
        description=(
            "Check the live GitHub Actions state of the required core and assets "
            "contexts for exactly one JN Engine pull request or branch. Missing "
            "contexts block the result and are never treated as successful."
        ),
        output_schema=CheckStatusOutput.model_json_schema(),
        annotations=LIVE_READ_ONLY_ANNOTATIONS,
    )
    def check_status(
        pr: int | None = None,
        branch: Annotated[str | None, Field(max_length=255)] = None,
        commit: Annotated[str | None, Field(max_length=40)] = None,
    ) -> CheckStatusOutput:
        try:
            return check_status_output(
                statuses.check(
                    pr=pr,
                    branch=branch,
                    commit=commit,
                    caller_identity=_caller_identity(),
                )
            )
        except CollaborationError as exc:
            raise tool_error(exc) from exc

    @server.tool(
        description=(
            "Propose a JN Engine change without a maintainer token: create a "
            "contrib/ branch from master with the provided UTF-8 files and open "
            "one pull request. Never pushes to master; requires an "
            "idempotency_key so retries return the same pull request. Supply "
            "expected_base_commit for snapshot-derived full-file replacements."
        ),
        output_schema=OpenPrOutput.model_json_schema(),
        annotations=PR_WRITE_ANNOTATIONS,
    )
    def open_pr(
        branch: Annotated[str, Field(min_length=9, max_length=89)],
        title: Annotated[str, Field(min_length=1, max_length=120)],
        files: list[dict],
        idempotency_key: Annotated[str, Field(min_length=8, max_length=64)],
        body: Annotated[str, Field(max_length=10_000)] = "",
        expected_base_commit: Annotated[
            str | None,
            Field(min_length=40, max_length=40),
        ] = None,
    ) -> OpenPrOutput:
        try:
            return open_pr_output(
                pr_writes.open_pr(
                    branch=branch,
                    title=title,
                    body=body,
                    files=files,
                    idempotency_key=idempotency_key,
                    caller_identity=_caller_identity(),
                    expected_base_commit=expected_base_commit,
                )
            )
        except CollaborationError as exc:
            raise tool_error(exc) from exc

    return server
