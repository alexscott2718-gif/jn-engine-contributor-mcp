"""Hermetic live-status fixtures, goldens, retries, errors, and audit contract."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from app.collaboration.audit import AuditLog
from app.collaboration.errors import CollaborationError
from app.collaboration.github import GitHubReadClient
from app.core.check_status import CheckStatusService
from app.models.status import check_status_output

FIXTURES = Path(__file__).parent / "fixtures" / "check_status"
GOLDENS = Path(__file__).parent / "goldens"
GREEN_SHA = "d98f2b75e683b2e8d75c7730a325047496cfda66"
RED_SHA = "c84a2d4d62760417a7fdb4d53ee72df1418c7b5a"
MASTER_SHA = "045497a242bd95a34786d7a33e948ae67d910e9e"
CHECKED_AT = datetime(2026, 7, 15, 4, 0, tzinfo=timezone.utc)
TEST_TOKEN = "unit-test-read-only-token-never-log-this"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _fixture(name: str) -> dict:
    return _json(FIXTURES / f"{name}.json")


def _golden(name: str) -> dict:
    return _json(GOLDENS / f"check_status_{name}.json")


def _handler(
    scenario: str,
    *,
    mutate_assets: bool = False,
    pull_failures: int = 0,
    pull_failure_status: int = 503,
    actions_not_found: bool = False,
    calls: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    state = {"pull_failures": pull_failures}

    def handle(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        assert request.method == "GET"
        assert request.headers["authorization"] == f"Bearer {TEST_TOKEN}"
        path = request.url.path
        if path.endswith("/git/ref/pull/6/head"):
            if state["pull_failures"]:
                state["pull_failures"] -= 1
                message = (
                    "You have exceeded a secondary rate limit"
                    if pull_failure_status == 403
                    else "temporary"
                )
                return httpx.Response(
                    pull_failure_status,
                    json={"message": message},
                )
            return httpx.Response(200, json=_fixture("green_pr_ref"))
        if path.endswith("/git/ref/pull/7/head"):
            return httpx.Response(200, json=_fixture("red_pr_ref"))
        if path.endswith("/git/ref/pull/999/head"):
            return httpx.Response(404, json={"message": "Not Found"})
        if path.endswith("/git/ref/heads/master"):
            return httpx.Response(200, json=_fixture("branch_master_ref"))
        if path.endswith(f"/commits/{GREEN_SHA}"):
            return httpx.Response(200, json=_fixture("green_commit"))
        if path.endswith("/actions/runs"):
            if actions_not_found:
                return httpx.Response(404, json={"message": "Not Found"})
            head_sha = request.url.params.get("head_sha")
            if head_sha == RED_SHA:
                return httpx.Response(200, json=_fixture("red_runs"))
            if head_sha == MASTER_SHA:
                return httpx.Response(200, json=_fixture("master_runs"))
            assert head_sha == GREEN_SHA
            fixture = "missing_runs" if scenario == "missing" else "green_runs"
            return httpx.Response(200, json=_fixture(fixture))
        if path.endswith("/actions/runs/29384925309/jobs"):
            fixture = "missing_jobs" if scenario == "missing" else "green_jobs"
            payload = _fixture(fixture)
            if mutate_assets:
                assets = next(job for job in payload["jobs"] if job["name"] == "assets")
                assets["conclusion"] = "failure"
            return httpx.Response(200, json=payload)
        if path.endswith("/actions/runs/29385174905/jobs"):
            return httpx.Response(200, json=_fixture("red_jobs"))
        if path.endswith("/actions/runs/29385688711/jobs"):
            return httpx.Response(200, json=_fixture("master_jobs"))
        if path.endswith("/actions/runs/29384925309/artifacts"):
            fixture = (
                "missing_artifacts" if scenario == "missing" else "green_artifacts"
            )
            return httpx.Response(200, json=_fixture(fixture))
        if path.endswith("/actions/runs/29385174905/artifacts"):
            return httpx.Response(200, json=_fixture("red_artifacts"))
        if path.endswith("/actions/runs/29385688711/artifacts"):
            return httpx.Response(200, json=_fixture("master_artifacts"))
        raise AssertionError(f"unexpected hermetic request: {request.url}")

    return httpx.MockTransport(handle)


def _service(
    tmp_path: Path,
    scenario: str,
    *,
    mutate_assets: bool = False,
    pull_failures: int = 0,
    pull_failure_status: int = 503,
    actions_not_found: bool = False,
    sleeper=lambda _seconds: None,
    token_loader=lambda: TEST_TOKEN,
    calls: list[httpx.Request] | None = None,
) -> tuple[CheckStatusService, Path]:
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(mode=0o700)
    audit_path = audit_dir / "tool_calls.ndjson"
    client = httpx.Client(
        base_url="https://api.github.test",
        transport=_handler(
            scenario,
            mutate_assets=mutate_assets,
            pull_failures=pull_failures,
            pull_failure_status=pull_failure_status,
            actions_not_found=actions_not_found,
            calls=calls,
        ),
    )
    github = GitHubReadClient(token_loader, client=client, sleeper=sleeper)
    return (
        CheckStatusService(github, AuditLog(audit_path), clock=lambda: CHECKED_AT),
        audit_path,
    )


@pytest.mark.parametrize(
    ("scenario", "pr"),
    [("green", 6), ("red", 7), ("missing", 6)],
)
def test_real_fixtures_match_full_structured_goldens(
    tmp_path: Path,
    scenario: str,
    pr: int,
):
    service, audit_path = _service(tmp_path, scenario)
    result = service.check(
        pr=pr,
        branch=None,
        commit=None,
        caller_identity="github:fixture-user",
    )
    actual = check_status_output(result).model_dump(mode="json")
    assert actual == _golden(scenario)

    records = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert records == [
        {
            "timestamp": "2026-07-15T04:00:00Z",
            "caller_identity": "github:fixture-user",
            "tool": "check_status",
            "args": {"pr": pr, "branch": None, "commit": None},
            "resolved_commit": actual["commit"],
            "outcome": _golden(scenario)["overall"],
            "github_api_status": [200, 200, 200, 200],
        }
    ]
    assert TEST_TOKEN not in audit_path.read_text(encoding="utf-8")
    assert audit_path.stat().st_mode & 0o777 == 0o600


def test_branch_defaults_to_its_resolved_head(tmp_path: Path):
    service, _audit_path = _service(tmp_path, "green")
    result = service.check(
        pr=None,
        branch="master",
        commit=None,
        caller_identity="device:test",
    )
    output = check_status_output(result).model_dump(mode="json")
    assert output["ref"] == "refs/heads/master"
    assert output["commit"] == MASTER_SHA
    assert output["overall"] == "success"


def test_explicit_commit_override_is_verified_before_status_lookup(tmp_path: Path):
    service, _audit_path = _service(tmp_path, "green")
    result = service.check(
        pr=None,
        branch="master",
        commit=GREEN_SHA,
        caller_identity="device:test",
    )
    output = check_status_output(result).model_dump(mode="json")
    assert output["commit"] == GREEN_SHA
    assert output["overall"] == "success"


@pytest.mark.parametrize(
    ("pr", "branch"),
    [(None, None), (6, "master")],
)
def test_both_or_neither_selector_is_bad_args_and_audited(
    tmp_path: Path,
    pr: int | None,
    branch: str | None,
):
    service, audit_path = _service(tmp_path, "green")
    with pytest.raises(CollaborationError) as raised:
        service.check(
            pr=pr,
            branch=branch,
            commit=None,
            caller_identity="github:fixture-user",
        )
    assert raised.value.code == "bad_args"
    record = json.loads(audit_path.read_text(encoding="utf-8"))
    assert record["outcome"] == "bad_args"
    assert record["github_api_status"] == []


def test_missing_credential_fails_closed_and_is_audited(tmp_path: Path):
    def missing_token() -> str:
        raise ValueError("missing test token")

    service, audit_path = _service(
        tmp_path,
        "green",
        token_loader=missing_token,
    )
    with pytest.raises(CollaborationError) as raised:
        service.check(
            pr=6,
            branch=None,
            commit=None,
            caller_identity="github:fixture-user",
        )
    assert raised.value.code == "credential_unavailable"
    record = json.loads(audit_path.read_text(encoding="utf-8"))
    assert record["outcome"] == "credential_unavailable"
    assert record["resolved_commit"] is None


def test_transient_github_failures_retry_with_backoff_then_return_whole_result(
    tmp_path: Path,
):
    sleeps: list[float] = []
    service, audit_path = _service(
        tmp_path,
        "green",
        pull_failures=2,
        sleeper=sleeps.append,
    )
    result = service.check(
        pr=6,
        branch=None,
        commit=None,
        caller_identity="github:fixture-user",
    )
    assert result.overall == "success"
    assert sleeps == [0.25, 0.75]
    record = json.loads(audit_path.read_text(encoding="utf-8"))
    assert record["github_api_status"] == [503, 503, 200, 200, 200, 200]


def test_secondary_rate_limit_retries_with_backoff(tmp_path: Path):
    sleeps: list[float] = []
    service, audit_path = _service(
        tmp_path,
        "green",
        pull_failures=1,
        pull_failure_status=403,
        sleeper=sleeps.append,
    )
    result = service.check(
        pr=6,
        branch=None,
        commit=None,
        caller_identity="github:fixture-user",
    )
    assert result.overall == "success"
    assert sleeps == [0.25]
    record = json.loads(audit_path.read_text(encoding="utf-8"))
    assert record["github_api_status"] == [403, 200, 200, 200, 200]


def test_exhausted_retry_returns_no_partial_result_and_is_audited(tmp_path: Path):
    service, audit_path = _service(tmp_path, "green", pull_failures=3)
    with pytest.raises(CollaborationError) as raised:
        service.check(
            pr=6,
            branch=None,
            commit=None,
            caller_identity="github:fixture-user",
        )
    assert raised.value.code == "upstream_unavailable"
    record = json.loads(audit_path.read_text(encoding="utf-8"))
    assert record["outcome"] == "upstream_unavailable"
    assert record["resolved_commit"] is None
    assert record["github_api_status"] == [503, 503, 503]


def test_expired_github_credential_is_typed_and_not_retried(tmp_path: Path):
    service, audit_path = _service(
        tmp_path,
        "green",
        pull_failures=1,
        pull_failure_status=401,
    )
    with pytest.raises(CollaborationError) as raised:
        service.check(
            pr=6,
            branch=None,
            commit=None,
            caller_identity="github:fixture-user",
        )
    assert raised.value.code == "credential_unavailable"
    record = json.loads(audit_path.read_text(encoding="utf-8"))
    assert record["github_api_status"] == [401]


def test_not_found_is_typed_and_audited(tmp_path: Path):
    service, audit_path = _service(tmp_path, "green")
    with pytest.raises(CollaborationError) as raised:
        service.check(
            pr=999,
            branch=None,
            commit=None,
            caller_identity="github:fixture-user",
        )
    assert raised.value.code == "not_found"
    record = json.loads(audit_path.read_text(encoding="utf-8"))
    assert record["outcome"] == "not_found"
    assert record["github_api_status"] == [404]


def test_actions_permission_masked_as_404_is_credential_unavailable(tmp_path: Path):
    service, audit_path = _service(tmp_path, "green", actions_not_found=True)
    with pytest.raises(CollaborationError) as raised:
        service.check(
            pr=6,
            branch=None,
            commit=None,
            caller_identity="github:fixture-user",
        )
    assert raised.value.code == "credential_unavailable"
    record = json.loads(audit_path.read_text(encoding="utf-8"))
    assert record["resolved_commit"] == GREEN_SHA
    assert record["github_api_status"] == [200, 404]


def test_unavailable_audit_sink_fails_a_complete_status_call_closed(tmp_path: Path):
    service, audit_path = _service(tmp_path, "green")
    audit_path.parent.rmdir()
    with pytest.raises(CollaborationError) as raised:
        service.check(
            pr=6,
            branch=None,
            commit=None,
            caller_identity="github:fixture-user",
        )
    assert raised.value.code == "upstream_unavailable"
    assert not audit_path.exists()


def test_fixture_mutation_breaks_the_green_golden(tmp_path: Path):
    service, _audit_path = _service(tmp_path, "green", mutate_assets=True)
    mutated = check_status_output(
        service.check(
            pr=6,
            branch=None,
            commit=None,
            caller_identity="github:mutation-test",
        )
    ).model_dump(mode="json")
    with pytest.raises(AssertionError, match="mutated assets fixture"):
        assert mutated == _golden("green"), "mutated assets fixture matched green golden"
    print("fixture flipped assets success->failure: golden assertion failed as required")
    assert mutated["required_contexts"]["assets"]["state"] == "failure"
    assert mutated["overall"] == "failure"
