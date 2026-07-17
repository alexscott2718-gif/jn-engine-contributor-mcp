"""Hermetic PR-write fixtures: validation, idempotency, PR-only, and audit."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from app.collaboration.audit import AuditLog
from app.collaboration.errors import CollaborationError
from app.collaboration.github import GitHubWriteClient
from app.core.open_pr import OpenPrService, WriteDisabledOpenPrService

BASE_SHA = "045497a242bd95a34786d7a33e948ae67d910e9e"
BASE_TREE = "1111111111111111111111111111111111111111"
NEW_TREE = "2222222222222222222222222222222222222222"
NEW_COMMIT = "3333333333333333333333333333333333333333"
OPENED_AT = datetime(2026, 7, 17, 4, 0, tzinfo=timezone.utc)
TEST_TOKEN = "unit-test-pr-write-token-never-log-this"
KEY = "session-2026-07-17-a"
CALLER = "github:contributor"

VALID = {
    "branch": "contrib/menu-fix",
    "title": "Fix menu text kerning",
    "body": "Kerning table was off by one.",
    "files": [{"path": "src/game/menu.c", "content": "int x;\n"}],
    "idempotency_key": KEY,
}


def _args(**overrides):
    merged = {**VALID, **overrides}
    merged.setdefault("caller_identity", CALLER)
    return merged


class _State:
    def __init__(
        self,
        *,
        branch_exists: bool = False,
        head_key: str | None = None,
        open_pr_exists: bool = False,
        pr_create_conflict: bool = False,
    ) -> None:
        self.branch_exists = branch_exists
        self.head_key = head_key
        self.open_pr_exists = open_pr_exists
        self.pr_create_conflict = pr_create_conflict
        self.requests: list[httpx.Request] = []


def _handler(state: _State) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        assert request.headers["authorization"] == f"Bearer {TEST_TOKEN}"
        path = request.url.path
        if request.method == "GET":
            if path.endswith("/git/ref/heads/master"):
                return httpx.Response(
                    200, json={"object": {"sha": BASE_SHA, "type": "commit"}}
                )
            if path.endswith("/git/ref/heads/contrib/menu-fix"):
                if not state.branch_exists:
                    return httpx.Response(404, json={"message": "Not Found"})
                return httpx.Response(
                    200, json={"object": {"sha": NEW_COMMIT, "type": "commit"}}
                )
            if path.endswith(f"/git/commits/{BASE_SHA}"):
                return httpx.Response(
                    200, json={"sha": BASE_SHA, "tree": {"sha": BASE_TREE}}
                )
            if path.endswith(f"/git/commits/{NEW_COMMIT}"):
                trailer = (
                    f"\n\nProposed-by: {CALLER}\nIdempotency-Key: {state.head_key}\n"
                    if state.head_key
                    else "\n"
                )
                return httpx.Response(
                    200,
                    json={"sha": NEW_COMMIT, "message": f"prior work{trailer}"},
                )
            if path.endswith("/pulls"):
                items = (
                    [{"number": 12, "html_url": "https://example.invalid/pull/12"}]
                    if state.open_pr_exists
                    else []
                )
                return httpx.Response(200, json=items)
            raise AssertionError(f"unexpected GET {path}")
        assert request.method == "POST"
        body = json.loads(request.content.decode("utf-8"))
        if path.endswith("/git/trees"):
            assert body["base_tree"] == BASE_TREE
            assert all(entry["mode"] == "100644" for entry in body["tree"])
            return httpx.Response(201, json={"sha": NEW_TREE})
        if path.endswith("/git/commits"):
            assert f"Idempotency-Key: {KEY}" in body["message"]
            assert f"Proposed-by: {CALLER}" in body["message"]
            assert body["parents"] == [BASE_SHA]
            return httpx.Response(201, json={"sha": NEW_COMMIT})
        if path.endswith("/git/refs"):
            assert body["ref"] == "refs/heads/contrib/menu-fix"
            assert body["ref"] != "refs/heads/master"
            return httpx.Response(201, json={"ref": body["ref"]})
        if path.endswith("/pulls"):
            assert body["base"] == "master"
            assert body["head"] == "contrib/menu-fix"
            if state.pr_create_conflict:
                return httpx.Response(
                    422, json={"message": "A pull request already exists"}
                )
            return httpx.Response(
                201,
                json={"number": 12, "html_url": "https://example.invalid/pull/12"},
            )
        raise AssertionError(f"unexpected POST {path}")

    return httpx.MockTransport(handle)


def _service(state: _State, tmp_path: Path) -> tuple[OpenPrService, Path]:
    audit_path = tmp_path / "tool_calls.ndjson"
    client = GitHubWriteClient(
        lambda: TEST_TOKEN,
        client=httpx.Client(
            base_url="https://api.github.invalid",
            transport=_handler(state),
        ),
        sleeper=lambda _: None,
    )
    service = OpenPrService(
        client,
        AuditLog(audit_path),
        clock=lambda: OPENED_AT,
    )
    return service, audit_path


def _audit_records(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def test_happy_path_creates_branch_and_opens_pr(tmp_path: Path):
    state = _State()
    service, audit_path = _service(state, tmp_path)
    result = service.open_pr(**_args())
    assert result.pr_number == 12
    assert result.created_branch is True
    assert result.replayed is False
    assert result.base_commit == BASE_SHA
    assert result.head_commit == NEW_COMMIT
    assert result.base_ref == "refs/heads/master"
    record = _audit_records(audit_path)[-1]
    assert record["outcome"] == "opened"
    assert record["caller_identity"] == CALLER
    assert record["pr_number"] == 12
    assert record["args"]["files"] == [{"path": "src/game/menu.c", "chars": 7}]
    text = audit_path.read_text(encoding="utf-8")
    assert TEST_TOKEN not in text
    assert "int x;" not in text


def test_replay_with_same_key_returns_existing_pr_without_new_commits(
    tmp_path: Path,
):
    state = _State(branch_exists=True, head_key=KEY, open_pr_exists=True)
    service, audit_path = _service(state, tmp_path)
    result = service.open_pr(**_args())
    assert result.replayed is True
    assert result.created_branch is False
    assert result.pr_number == 12
    assert all(request.method == "GET" or request.url.path.endswith("/pulls")
               for request in state.requests)
    assert not any(
        request.method == "POST" and "/git/" in request.url.path
        for request in state.requests
    )
    assert _audit_records(audit_path)[-1]["outcome"] == "replayed"


def test_existing_branch_with_different_key_is_conflict(tmp_path: Path):
    state = _State(branch_exists=True, head_key="other-key-0001")
    service, audit_path = _service(state, tmp_path)
    with pytest.raises(CollaborationError) as info:
        service.open_pr(**_args())
    assert info.value.code == "conflict"
    assert _audit_records(audit_path)[-1]["outcome"] == "conflict"


def test_duplicate_pr_creation_race_resolves_to_the_existing_pr(tmp_path: Path):
    state = _State(pr_create_conflict=True, open_pr_exists=True)
    service, _ = _service(state, tmp_path)
    result = service.open_pr(**_args())
    assert result.pr_number == 12


@pytest.mark.parametrize(
    "branch",
    [
        "master",
        "main",
        "contrib/../master",
        "contrib/UPPER",
        "refs/heads/master",
        "contrib/",
        "feature/x",
        "contrib/a.lock",
    ],
)
def test_branch_allowlist_rejects_everything_outside_contrib(
    tmp_path: Path, branch: str
):
    state = _State()
    service, audit_path = _service(state, tmp_path)
    with pytest.raises(CollaborationError) as info:
        service.open_pr(**_args(branch=branch))
    assert info.value.code == "bad_args"
    assert state.requests == []
    assert _audit_records(audit_path)[-1]["outcome"] == "bad_args"


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../outside.c",
        "src/../../x",
        ".git/config",
        ".github/workflows/ci.yml",
        "src\\game\\menu.c",
        "src//menu.c",
        "src/./menu.c",
        "",
    ],
)
def test_path_validation_rejects_traversal_git_and_workflow_writes(
    tmp_path: Path, path: str
):
    state = _State()
    service, _ = _service(state, tmp_path)
    with pytest.raises(CollaborationError) as info:
        service.open_pr(**_args(files=[{"path": path, "content": "x"}]))
    assert info.value.code == "bad_args"
    assert state.requests == []


def test_missing_or_malformed_idempotency_key_is_bad_args(tmp_path: Path):
    state = _State()
    service, _ = _service(state, tmp_path)
    for key in ("", "short", "bad key", None, "x" * 65):
        with pytest.raises(CollaborationError) as info:
            service.open_pr(**_args(idempotency_key=key))
        assert info.value.code == "bad_args"
    assert state.requests == []


def test_file_and_size_bounds_are_enforced(tmp_path: Path):
    state = _State()
    service, _ = _service(state, tmp_path)
    too_many = [
        {"path": f"src/f{index}.c", "content": "x"} for index in range(33)
    ]
    for files in ([], too_many, [{"path": "a.c", "content": "x" * 200_001}]):
        with pytest.raises(CollaborationError) as info:
            service.open_pr(**_args(files=files))
        assert info.value.code == "bad_args"
    assert state.requests == []


def test_missing_credential_fails_closed_and_is_audited(tmp_path: Path):
    audit_path = tmp_path / "tool_calls.ndjson"
    client = GitHubWriteClient(
        lambda: (_ for _ in ()).throw(OSError("no secret")),
        client=httpx.Client(
            base_url="https://api.github.invalid",
            transport=_handler(_State()),
        ),
        sleeper=lambda _: None,
    )
    service = OpenPrService(client, AuditLog(audit_path), clock=lambda: OPENED_AT)
    with pytest.raises(CollaborationError) as info:
        service.open_pr(**_args())
    assert info.value.code == "credential_unavailable"
    assert _audit_records(audit_path)[-1]["outcome"] == "credential_unavailable"


def test_mutation_failures_never_retry_and_fail_the_whole_call(tmp_path: Path):
    posts: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if request.url.path.endswith("/git/ref/heads/master"):
                return httpx.Response(200, json={"object": {"sha": BASE_SHA}})
            if request.url.path.endswith("/git/ref/heads/contrib/menu-fix"):
                return httpx.Response(404, json={"message": "Not Found"})
            if request.url.path.endswith(f"/git/commits/{BASE_SHA}"):
                return httpx.Response(
                    200, json={"sha": BASE_SHA, "tree": {"sha": BASE_TREE}}
                )
        posts.append(request.url.path)
        return httpx.Response(502, json={"message": "bad gateway"})

    audit_path = tmp_path / "tool_calls.ndjson"
    client = GitHubWriteClient(
        lambda: TEST_TOKEN,
        client=httpx.Client(
            base_url="https://api.github.invalid",
            transport=httpx.MockTransport(handle),
        ),
        sleeper=lambda _: None,
    )
    service = OpenPrService(client, AuditLog(audit_path), clock=lambda: OPENED_AT)
    with pytest.raises(CollaborationError) as info:
        service.open_pr(**_args())
    assert info.value.code == "upstream_unavailable"
    assert len(posts) == 1
    assert _audit_records(audit_path)[-1]["outcome"] == "upstream_unavailable"


def test_unavailable_audit_sink_fails_a_complete_call_closed(tmp_path: Path):
    state = _State()
    audit_dir = tmp_path / "missing"
    client = GitHubWriteClient(
        lambda: TEST_TOKEN,
        client=httpx.Client(
            base_url="https://api.github.invalid",
            transport=_handler(state),
        ),
        sleeper=lambda _: None,
    )
    service = OpenPrService(
        client,
        AuditLog(audit_dir / "tool_calls.ndjson"),
        clock=lambda: OPENED_AT,
    )
    with pytest.raises(CollaborationError) as info:
        service.open_pr(**_args())
    assert info.value.code == "upstream_unavailable"


def test_write_disabled_service_fails_closed(tmp_path: Path):
    service = WriteDisabledOpenPrService()
    with pytest.raises(CollaborationError) as info:
        service.open_pr(**_args())
    assert info.value.code == "write_disabled"
