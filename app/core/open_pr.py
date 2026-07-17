"""Fail-closed, PR-only branch and pull-request creation for JN Engine.

The service holds the only write path in the gateway. It can create or update a
`contrib/*` branch and open a pull request against `master`; it is structurally
incapable of pushing to `master` because the only ref it ever creates or reads
for update is the validated `refs/heads/contrib/...` ref, and the pull-request
base is a code-owned constant. Repository branch protection (strict `core` and
`assets` contexts, admins included) remains the independent second layer.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from app.collaboration.audit import AuditLog, AuditUnavailableError
from app.collaboration.errors import (
    CollaborationError,
    bad_args,
    conflict,
    not_found,
    upstream_unavailable,
    write_disabled,
)
from app.collaboration.github import GitHubWriteClient, GitHubWriteSession
from app.config import EXPECTED_REPOSITORY

PROTECTED_BASE_BRANCH = "master"
BRANCH_PREFIX = "contrib/"
MAX_FILES = 32
MAX_FILE_CHARS = 200_000
MAX_TOTAL_CHARS = 1_000_000
MAX_PATH_CHARS = 300

_BRANCH = re.compile(r"^contrib/[a-z0-9][a-z0-9._-]{0,80}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_IDEMPOTENCY_TRAILER = re.compile(r"^Idempotency-Key: ([A-Za-z0-9._-]{8,64})$", re.M)
_FORBIDDEN_PATH_PREFIXES = (".git/", ".github/workflows/")
_FORBIDDEN_PATH_EXACT = (".git", ".github/workflows")


@dataclass(frozen=True)
class ProposedFile:
    path: str
    content: str


@dataclass(frozen=True)
class OpenedPr:
    repository: str
    base_ref: str
    base_commit: str
    branch: str
    head_commit: str
    pr_number: int
    pr_url: str
    created_branch: bool
    replayed: bool
    opened_at: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _validate_branch(branch: object) -> str:
    if not isinstance(branch, str) or _BRANCH.fullmatch(branch) is None:
        raise bad_args(
            "branch must match contrib/<name> using lowercase letters, digits, "
            "dot, underscore, or hyphen"
        )
    if ".." in branch or branch.endswith(".lock") or branch.endswith("."):
        raise bad_args("branch is not a valid git ref name")
    return branch


def _validate_title(title: object) -> str:
    if (
        not isinstance(title, str)
        or not title
        or len(title) > 120
        or title != title.strip()
        or _CONTROL.search(title)
    ):
        raise bad_args("title must be 1..120 printable characters")
    return title


def _validate_body(body: object) -> str:
    if not isinstance(body, str) or len(body) > 10_000:
        raise bad_args("body must be at most 10000 characters")
    if _CONTROL.sub("", body.replace("\n", "").replace("\t", "")) != body.replace(
        "\n", ""
    ).replace("\t", ""):
        raise bad_args("body contains forbidden control characters")
    return body


def _validate_idempotency_key(key: object) -> str:
    if not isinstance(key, str) or _IDEMPOTENCY_KEY.fullmatch(key) is None:
        raise bad_args(
            "idempotency_key must be 8..64 characters of letters, digits, "
            "dot, underscore, or hyphen"
        )
    return key


def _validate_path(path: object) -> str:
    if not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS:
        raise bad_args("every file path must be 1..300 characters")
    if _CONTROL.search(path) or "\\" in path:
        raise bad_args("file paths must not contain control characters or backslashes")
    if path.startswith("/") or path.endswith("/"):
        raise bad_args("file paths must be repository-relative")
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise bad_args("file paths must not contain empty or dot segments")
    if path in _FORBIDDEN_PATH_EXACT or any(
        path.startswith(prefix) for prefix in _FORBIDDEN_PATH_PREFIXES
    ):
        raise bad_args("file paths may not modify .git or workflow definitions")
    return path


def _validate_files(files: object) -> tuple[ProposedFile, ...]:
    if not isinstance(files, (list, tuple)) or not files or len(files) > MAX_FILES:
        raise bad_args(f"files must contain 1..{MAX_FILES} entries")
    seen: set[str] = set()
    validated: list[ProposedFile] = []
    total = 0
    for entry in files:
        if isinstance(entry, ProposedFile):
            path, content = entry.path, entry.content
        elif isinstance(entry, dict):
            path, content = entry.get("path"), entry.get("content")
        else:
            raise bad_args("every file entry must provide path and content")
        path = _validate_path(path)
        if path in seen:
            raise bad_args("duplicate file path")
        if not isinstance(content, str):
            raise bad_args("every file content must be UTF-8 text")
        if len(content) > MAX_FILE_CHARS:
            raise bad_args(f"file content is limited to {MAX_FILE_CHARS} characters")
        total += len(content)
        if total > MAX_TOTAL_CHARS:
            raise bad_args(
                f"combined file content is limited to {MAX_TOTAL_CHARS} characters"
            )
        seen.add(path)
        validated.append(ProposedFile(path=path, content=content))
    return tuple(validated)


def _require_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise upstream_unavailable()
    return value


def _require_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise upstream_unavailable()
    return value


def _require_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise upstream_unavailable()
    return value


class OpenPrService:
    def __init__(
        self,
        github: GitHubWriteClient,
        audit: AuditLog,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._github = github
        self._audit = audit
        self._clock = clock

    def close(self) -> None:
        self._github.close()

    # -- GitHub steps -------------------------------------------------------

    def _base_commit(self, session: GitHubWriteSession) -> str:
        payload = session.get(
            f"/repos/{EXPECTED_REPOSITORY}/git/ref/heads/{PROTECTED_BASE_BRANCH}"
        )
        return _require_string(_require_dict(payload.get("object")).get("sha")).lower()

    def _existing_branch_head(
        self, session: GitHubWriteSession, branch: str
    ) -> str | None:
        try:
            payload = session.get(
                f"/repos/{EXPECTED_REPOSITORY}/git/ref/heads/"
                f"{quote(branch, safe='')}"
            )
        except CollaborationError as exc:
            if exc.code == "not_found":
                return None
            raise
        return _require_string(_require_dict(payload.get("object")).get("sha")).lower()

    def _head_idempotency_key(
        self, session: GitHubWriteSession, commit: str
    ) -> str | None:
        payload = session.get(
            f"/repos/{EXPECTED_REPOSITORY}/git/commits/{commit}"
        )
        message = payload.get("message")
        if not isinstance(message, str):
            raise upstream_unavailable()
        match = _IDEMPOTENCY_TRAILER.search(message)
        return match.group(1) if match else None

    def _create_commit(
        self,
        session: GitHubWriteSession,
        *,
        base_commit: str,
        files: tuple[ProposedFile, ...],
        title: str,
        caller_identity: str,
        idempotency_key: str,
    ) -> str:
        base_payload = session.get(
            f"/repos/{EXPECTED_REPOSITORY}/git/commits/{base_commit}"
        )
        base_tree = _require_string(
            _require_dict(base_payload.get("tree")).get("sha")
        )
        _, tree_payload = session.send(
            "POST",
            f"/repos/{EXPECTED_REPOSITORY}/git/trees",
            json_body={
                "base_tree": base_tree,
                "tree": [
                    {
                        "path": item.path,
                        "mode": "100644",
                        "type": "blob",
                        "content": item.content,
                    }
                    for item in files
                ],
            },
        )
        tree_sha = _require_string(tree_payload.get("sha"))
        message = (
            f"{title}\n\n"
            f"Proposed-by: {caller_identity}\n"
            f"Idempotency-Key: {idempotency_key}\n"
        )
        _, commit_payload = session.send(
            "POST",
            f"/repos/{EXPECTED_REPOSITORY}/git/commits",
            json_body={
                "message": message,
                "tree": tree_sha,
                "parents": [base_commit],
            },
        )
        return _require_string(commit_payload.get("sha")).lower()

    def _create_branch(
        self, session: GitHubWriteSession, branch: str, commit: str
    ) -> None:
        ref = f"refs/heads/{branch}"
        if not ref.startswith(f"refs/heads/{BRANCH_PREFIX}"):
            # Structural PR-only guarantee: no other ref is ever written.
            raise bad_args("only contrib/ branches may be written")
        session.send(
            "POST",
            f"/repos/{EXPECTED_REPOSITORY}/git/refs",
            json_body={"ref": ref, "sha": commit},
        )

    def _find_open_pr(
        self, session: GitHubWriteSession, branch: str
    ) -> tuple[int, str] | None:
        owner = EXPECTED_REPOSITORY.split("/", 1)[0]
        items = session.get_list(
            f"/repos/{EXPECTED_REPOSITORY}/pulls",
            params={
                "state": "open",
                "head": f"{owner}:{branch}",
                "base": PROTECTED_BASE_BRANCH,
                "per_page": 10,
            },
        )
        for raw in items:
            item = _require_dict(raw)
            return (
                _require_int(item.get("number")),
                _require_string(item.get("html_url")),
            )
        return None

    def _open_pr(
        self,
        session: GitHubWriteSession,
        *,
        branch: str,
        title: str,
        body: str,
    ) -> tuple[int, str]:
        status, payload = session.send(
            "POST",
            f"/repos/{EXPECTED_REPOSITORY}/pulls",
            json_body={
                "title": title,
                "body": body,
                "head": branch,
                "base": PROTECTED_BASE_BRANCH,
                "maintainer_can_modify": True,
                "draft": False,
            },
            handled=(422,),
        )
        if status == 422:
            existing = self._find_open_pr(session, branch)
            if existing is None:
                raise upstream_unavailable()
            return existing
        return (
            _require_int(payload.get("number")),
            _require_string(payload.get("html_url")),
        )

    # -- Entry point --------------------------------------------------------

    def open_pr(
        self,
        *,
        branch: object,
        title: object,
        body: object,
        files: object,
        idempotency_key: object,
        caller_identity: str,
    ) -> OpenedPr:
        opened_at = _timestamp(self._clock())
        session: GitHubWriteSession | None = None
        result: OpenedPr | None = None
        failure: CollaborationError | None = None
        audit_files: list[dict[str, object]] = []
        base_commit: str | None = None
        head_commit: str | None = None
        try:
            valid_branch = _validate_branch(branch)
            valid_title = _validate_title(title)
            valid_body = _validate_body(body)
            valid_files = _validate_files(files)
            valid_key = _validate_idempotency_key(idempotency_key)
            audit_files = [
                {"path": item.path, "chars": len(item.content)}
                for item in valid_files
            ]

            session = self._github.begin()
            base_commit = self._base_commit(session)
            existing_head = self._existing_branch_head(session, valid_branch)

            if existing_head is not None:
                existing_key = self._head_idempotency_key(session, existing_head)
                if existing_key != valid_key:
                    raise conflict(
                        "branch already exists with different proposed work; "
                        "choose a new contrib/ branch name"
                    )
                head_commit = existing_head
                created_branch = False
                replayed = True
            else:
                head_commit = self._create_commit(
                    session,
                    base_commit=base_commit,
                    files=valid_files,
                    title=valid_title,
                    caller_identity=caller_identity,
                    idempotency_key=valid_key,
                )
                self._create_branch(session, valid_branch, head_commit)
                created_branch = True
                replayed = False

            pr_number, pr_url = self._open_pr(
                session,
                branch=valid_branch,
                title=valid_title,
                body=valid_body,
            )
            result = OpenedPr(
                repository=EXPECTED_REPOSITORY,
                base_ref=f"refs/heads/{PROTECTED_BASE_BRANCH}",
                base_commit=base_commit,
                branch=valid_branch,
                head_commit=head_commit,
                pr_number=pr_number,
                pr_url=pr_url,
                created_branch=created_branch,
                replayed=replayed,
                opened_at=opened_at,
            )
        except CollaborationError as exc:
            failure = exc
        except Exception:
            failure = upstream_unavailable()

        audit_record = {
            "timestamp": opened_at,
            "caller_identity": caller_identity,
            "tool": "open_pr",
            "args": {
                "branch": branch if isinstance(branch, str) else None,
                "title": title if isinstance(title, str) else None,
                "body_chars": len(body) if isinstance(body, str) else None,
                "files": audit_files,
                "idempotency_key": (
                    idempotency_key if isinstance(idempotency_key, str) else None
                ),
            },
            "base_commit": base_commit,
            "head_commit": head_commit,
            "pr_number": result.pr_number if result is not None else None,
            "outcome": (
                failure.code
                if failure is not None
                else ("replayed" if result is not None and result.replayed else "opened")
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


class WriteDisabledOpenPrService:
    """Fail closed when the deployment has not enabled the PR write path."""

    def close(self) -> None:
        return None

    def open_pr(self, **_kwargs: object) -> OpenedPr:
        raise write_disabled()
