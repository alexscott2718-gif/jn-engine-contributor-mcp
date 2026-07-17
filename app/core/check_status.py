"""Live, fail-closed status aggregation for the protected engine CI contexts."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import quote

from app.collaboration.audit import AuditLog, AuditUnavailableError
from app.collaboration.errors import (
    CollaborationError,
    bad_args,
    credential_unavailable,
    upstream_unavailable,
)
from app.collaboration.github import GitHubReadClient, GitHubReadSession
from app.config import EXPECTED_REPOSITORY

RequiredContextName = Literal["core", "assets"]
ContextState = Literal[
    "queued",
    "in_progress",
    "success",
    "failure",
    "neutral",
    "cancelled",
    "timed_out",
    "missing",
]
OverallState = Literal[
    "success",
    "failure",
    "neutral",
    "cancelled",
    "timed_out",
    "in_progress",
    "queued",
    "blocked",
]

REQUIRED_CONTEXTS: tuple[RequiredContextName, ...] = ("core", "assets")
_COMMIT = re.compile(r"^[0-9a-fA-F]{40}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_CONCLUSION_STATES: dict[str, ContextState] = {
    "success": "success",
    "failure": "failure",
    "neutral": "neutral",
    "cancelled": "cancelled",
    "timed_out": "timed_out",
    "skipped": "neutral",
    "stale": "cancelled",
    "action_required": "failure",
    "startup_failure": "failure",
}


@dataclass(frozen=True)
class RequiredContext:
    state: ContextState
    run_id: int | None
    run_url: str | None
    started_at: str | None
    completed_at: str | None


@dataclass(frozen=True)
class StatusArtifact:
    name: str
    size_bytes: int
    download_url: str
    expired: bool


@dataclass(frozen=True)
class CheckStatus:
    repository: str
    ref: str
    commit: str
    required_contexts: dict[RequiredContextName, RequiredContext]
    overall: OverallState
    blocked_reason: str | None
    artifacts: tuple[StatusArtifact, ...]
    checked_at: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _validate_request(
    *,
    pr: int | None,
    branch: str | None,
    commit: str | None,
) -> None:
    if (pr is None) == (branch is None):
        raise bad_args("provide exactly one of pr or branch")
    if pr is not None and (isinstance(pr, bool) or not isinstance(pr, int) or pr < 1):
        raise bad_args("pr must be a positive integer")
    if branch is not None and (
        not isinstance(branch, str)
        or not branch
        or len(branch) > 255
        or branch != branch.strip()
        or _CONTROL.search(branch)
    ):
        raise bad_args("branch is invalid")
    if commit is not None and (
        not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None
    ):
        raise bad_args("commit must be a full 40-character SHA")


def _require_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise upstream_unavailable()
    return value


def _require_list(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise upstream_unavailable()
    return value


def _require_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise upstream_unavailable()
    return value


def _context_state(job: dict[str, Any]) -> ContextState:
    status = job.get("status")
    if status == "queued" or status == "waiting" or status == "pending":
        return "queued"
    if status == "in_progress" or status == "requested":
        return "in_progress"
    if status != "completed":
        return "failure"
    conclusion = job.get("conclusion")
    if not isinstance(conclusion, str):
        return "failure"
    return _CONCLUSION_STATES.get(conclusion, "failure")


def _overall(
    contexts: dict[RequiredContextName, RequiredContext],
) -> tuple[OverallState, str | None]:
    missing = [name for name in REQUIRED_CONTEXTS if contexts[name].state == "missing"]
    if missing:
        return (
            "blocked",
            f"required CI context(s) not reported: {', '.join(missing)}",
        )
    states = {context.state for context in contexts.values()}
    if states == {"success"}:
        return "success", None
    for blocking in (
        "failure",
        "timed_out",
        "cancelled",
        "in_progress",
        "queued",
        "neutral",
    ):
        if blocking in states:
            return blocking, None  # type: ignore[return-value]
    return "failure", None


class CheckStatusService:
    def __init__(
        self,
        github: GitHubReadClient,
        audit: AuditLog,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._github = github
        self._audit = audit
        self._clock = clock

    def close(self) -> None:
        self._github.close()

    def _resolve(
        self,
        session: GitHubReadSession,
        *,
        pr: int | None,
        branch: str | None,
        commit: str | None,
    ) -> tuple[str, str]:
        repository_path = f"/repos/{EXPECTED_REPOSITORY}"
        if pr is not None:
            payload = session.get(f"{repository_path}/git/ref/pull/{pr}/head")
            head = _require_dict(payload.get("object"))
            resolved = _require_string(head.get("sha"))
            ref = f"refs/pull/{pr}/head"
        else:
            assert branch is not None
            payload = session.get(
                f"{repository_path}/git/ref/heads/{quote(branch, safe='')}"
            )
            branch_commit = _require_dict(payload.get("object"))
            resolved = _require_string(branch_commit.get("sha"))
            ref = f"refs/heads/{branch}"

        selected = commit.lower() if commit is not None else resolved.lower()
        if _COMMIT.fullmatch(selected) is None:
            raise upstream_unavailable()
        if commit is not None:
            commit_payload = session.get(f"{repository_path}/commits/{selected}")
            selected = _require_string(commit_payload.get("sha")).lower()
        return ref, selected

    def _contexts_and_artifacts(
        self,
        session: GitHubReadSession,
        commit: str,
    ) -> tuple[
        dict[RequiredContextName, RequiredContext],
        tuple[StatusArtifact, ...],
    ]:
        repository_path = f"/repos/{EXPECTED_REPOSITORY}"
        runs_payload = session.get(
            f"{repository_path}/actions/runs",
            params={"head_sha": commit, "per_page": 100},
            not_found_is_credential=True,
        )
        total_runs = runs_payload.get("total_count")
        if not isinstance(total_runs, int) or total_runs < 0:
            raise upstream_unavailable()
        runs = [_require_dict(item) for item in _require_list(runs_payload.get("workflow_runs"))]
        runs.sort(
            key=lambda run: (str(run.get("created_at", "")), int(run.get("id", 0))),
            reverse=True,
        )

        found: dict[RequiredContextName, RequiredContext] = {}
        artifact_run_ids: dict[int, str] = {}
        for run in runs:
            run_id = run.get("id")
            run_url = run.get("html_url")
            if not isinstance(run_id, int) or not isinstance(run_url, str):
                raise upstream_unavailable()
            jobs_payload = session.get(
                f"{repository_path}/actions/runs/{run_id}/jobs",
                params={"filter": "latest", "per_page": 100},
                not_found_is_credential=True,
            )
            total_jobs = jobs_payload.get("total_count")
            jobs = _require_list(jobs_payload.get("jobs"))
            if (
                not isinstance(total_jobs, int)
                or total_jobs < 0
                or total_jobs > len(jobs)
            ):
                raise upstream_unavailable()
            for raw_job in jobs:
                job = _require_dict(raw_job)
                name = job.get("name")
                if name not in REQUIRED_CONTEXTS or name in found:
                    continue
                typed_name: RequiredContextName = name
                found[typed_name] = RequiredContext(
                    state=_context_state(job),
                    run_id=run_id,
                    run_url=run_url,
                    started_at=(
                        job.get("started_at")
                        if isinstance(job.get("started_at"), str)
                        else None
                    ),
                    completed_at=(
                        job.get("completed_at")
                        if isinstance(job.get("completed_at"), str)
                        else None
                    ),
                )
                artifact_run_ids[run_id] = run_url
            if len(found) == len(REQUIRED_CONTEXTS):
                break

        if len(found) < len(REQUIRED_CONTEXTS) and total_runs > len(runs):
            # Never label a context missing when the first bounded API page did not
            # contain every run GitHub says exists.
            raise upstream_unavailable()

        contexts = {
            name: found.get(
                name,
                RequiredContext(
                    state="missing",
                    run_id=None,
                    run_url=None,
                    started_at=None,
                    completed_at=None,
                ),
            )
            for name in REQUIRED_CONTEXTS
        }

        artifacts: dict[int, StatusArtifact] = {}
        for run_id in sorted(artifact_run_ids):
            payload = session.get(
                f"{repository_path}/actions/runs/{run_id}/artifacts",
                params={"per_page": 100},
                not_found_is_credential=True,
            )
            total_artifacts = payload.get("total_count")
            artifact_items = _require_list(payload.get("artifacts"))
            if (
                not isinstance(total_artifacts, int)
                or total_artifacts < 0
                or total_artifacts > len(artifact_items)
            ):
                raise upstream_unavailable()
            for raw_artifact in artifact_items:
                artifact = _require_dict(raw_artifact)
                artifact_id = artifact.get("id")
                name = artifact.get("name")
                size = artifact.get("size_in_bytes")
                url = artifact.get("archive_download_url")
                expired = artifact.get("expired")
                if (
                    not isinstance(artifact_id, int)
                    or not isinstance(name, str)
                    or not isinstance(size, int)
                    or not isinstance(url, str)
                    or not isinstance(expired, bool)
                ):
                    raise upstream_unavailable()
                artifacts[artifact_id] = StatusArtifact(
                    name=name,
                    size_bytes=size,
                    download_url=url,
                    expired=expired,
                )
        return contexts, tuple(
            sorted(artifacts.values(), key=lambda item: (item.name, item.download_url))
        )

    def check(
        self,
        *,
        pr: int | None,
        branch: str | None,
        commit: str | None,
        caller_identity: str,
    ) -> CheckStatus:
        checked_at = _timestamp(self._clock())
        session: GitHubReadSession | None = None
        resolved_commit: str | None = None
        result: CheckStatus | None = None
        failure: CollaborationError | None = None
        try:
            _validate_request(pr=pr, branch=branch, commit=commit)
            session = self._github.begin()
            ref, resolved_commit = self._resolve(
                session,
                pr=pr,
                branch=branch,
                commit=commit,
            )
            contexts, artifacts = self._contexts_and_artifacts(
                session, resolved_commit
            )
            overall, blocked_reason = _overall(contexts)
            result = CheckStatus(
                repository=EXPECTED_REPOSITORY,
                ref=ref,
                commit=resolved_commit,
                required_contexts=contexts,
                overall=overall,
                blocked_reason=blocked_reason,
                artifacts=artifacts,
                checked_at=checked_at,
            )
        except CollaborationError as exc:
            failure = exc
        except Exception:
            failure = upstream_unavailable()

        audit_record = {
            "timestamp": checked_at,
            "caller_identity": caller_identity,
            "tool": "check_status",
            "args": {"pr": pr, "branch": branch, "commit": commit},
            "resolved_commit": resolved_commit,
            "outcome": (
                failure.code
                if failure is not None
                else result.overall if result is not None else "upstream_unavailable"
            ),
            "github_api_status": session.statuses if session is not None else [],
        }
        try:
            self._audit.append(audit_record)
        except AuditUnavailableError as exc:
            raise upstream_unavailable() from exc
        if failure is not None:
            raise failure
        if result is None:
            raise upstream_unavailable()
        return result


class CredentialUnavailableStatusService:
    """Fail closed when local development has no live GitHub credential."""

    def close(self) -> None:
        return None

    def check(
        self,
        *,
        pr: int | None,
        branch: str | None,
        commit: str | None,
        caller_identity: str,
    ) -> CheckStatus:
        del pr, branch, commit, caller_identity
        raise credential_unavailable()
