"""Fail-fast configuration contract."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import ConfigError, Settings
from app.run import main as run_main
from tests.conftest import make_github_settings


def test_authless_local_requires_loopback(snapshot: Path):
    settings = Settings(
        auth_mode="authless_local",
        api_host="127.0.0.1",
        public_base_url="",
        jn_snapshot_path=snapshot,
    )
    assert settings.auth_mode == "authless_local"

    with pytest.raises(ValidationError, match="loopback"):
        Settings(
            auth_mode="authless_local",
            api_host="0.0.0.0",
            public_base_url="",
            jn_snapshot_path=snapshot,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("expected_repository", "someone/else", "manifest assertion"),
        ("expected_ref", "refs/heads/main", "manifest assertion"),
        ("enable_shell_actions", True, "forbidden"),
    ],
)
def test_hard_safety_values_fail(
    tmp_path: Path,
    snapshot: Path,
    field: str,
    value: object,
    message: str,
):
    with pytest.raises(ValidationError, match=message):
        make_github_settings(tmp_path, snapshot, **{field: value})


def test_write_actions_require_the_dedicated_pr_write_credential(
    tmp_path: Path,
    snapshot: Path,
):
    from tests.conftest import write_private

    with pytest.raises(ValidationError):
        make_github_settings(tmp_path, snapshot, enable_write_actions=True)
    token_file = write_private(
        tmp_path / "secrets" / "github_pr_write_token",
        b"unit-test-pr-write-token-not-real-1234",
    )
    settings = make_github_settings(
        tmp_path,
        snapshot,
        enable_write_actions=True,
        github_pr_write_token_file=token_file,
    )
    assert settings.enable_write_actions is True
    assert settings.github_pr_write_token().startswith("unit-test-pr-write")


def test_write_actions_stay_forbidden_for_authless_local(
    tmp_path: Path,
    snapshot: Path,
):
    with pytest.raises(ValidationError, match="authenticated"):
        Settings(
            auth_mode="authless_local",
            api_host="127.0.0.1",
            jn_snapshot_path=snapshot,
            enable_write_actions=True,
        )


def test_expected_values_cannot_redirect_provider(tmp_path: Path, snapshot: Path):
    settings = make_github_settings(tmp_path, snapshot)
    assert settings.expected_repository == "alexscott2718-gif/jn-engine"
    assert settings.expected_ref == "refs/heads/master"


def test_production_requires_https_and_nonempty_redirect_allowlist(
    tmp_path: Path,
    snapshot: Path,
):
    with pytest.raises(ValidationError, match="HTTPS"):
        make_github_settings(
            tmp_path,
            snapshot,
            app_env="production",
            public_base_url="http://gateway.example.test",
        )

    with pytest.raises(ValidationError, match="must not be empty"):
        make_github_settings(
            tmp_path,
            snapshot,
            app_env="production",
            public_base_url="https://gateway.example.test",
            oauth_allowed_client_redirect_uris=(),
        )


@pytest.mark.parametrize(
    "redirect",
    [
        "https://*.example.test/callback",
        "custom-app://callback",
        "http://example.test/callback",
        "https://user@example.test/callback",
        "https://example.test/callback#fragment",
    ],
)
def test_unsafe_redirect_patterns_fail(
    tmp_path: Path,
    snapshot: Path,
    redirect: str,
):
    with pytest.raises(ValidationError, match="OAuth redirect"):
        make_github_settings(
            tmp_path,
            snapshot,
            oauth_allowed_client_redirect_uris=(redirect,),
        )


def test_secret_file_must_be_mode_0600(github_settings: Settings):
    github_settings.github_collaborator_token_file.chmod(0o644)
    with pytest.raises(ValidationError, match="0600"):
        Settings(**github_settings.model_dump())


def test_secret_file_cannot_escape_secrets_directory(
    tmp_path: Path,
    snapshot: Path,
):
    outside = tmp_path / "outside-token"
    outside.write_text("github_pat_outside_token_value_12345", encoding="utf-8")
    outside.chmod(0o600)
    with pytest.raises(ValidationError, match="inside GATEWAY_SECRETS_DIR"):
        make_github_settings(
            tmp_path,
            snapshot,
            github_collaborator_token_file=outside,
        )


def test_actions_read_credential_is_distinct_and_confined(
    tmp_path: Path,
    snapshot: Path,
):
    outside = tmp_path / "actions-read-token"
    outside.write_text("unit-test-read-only-token-value-12345", encoding="utf-8")
    outside.chmod(0o600)
    with pytest.raises(ValidationError, match="inside GATEWAY_SECRETS_DIR"):
        make_github_settings(
            tmp_path,
            snapshot,
            github_actions_read_token_file=outside,
        )


def test_audit_parent_must_be_private_and_durable(github_settings: Settings):
    github_settings.audit_log_path.parent.chmod(0o755)
    with pytest.raises(ValidationError, match="mode 0700"):
        Settings(**github_settings.model_dump())


def test_claim_ledger_is_distinct_but_uses_the_durable_audit_directory(
    github_settings: Settings,
    tmp_path: Path,
):
    assert github_settings.task_claim_ledger_path.name == "task_claims.ndjson"
    assert github_settings.task_claim_ledger_path.parent == (
        github_settings.audit_log_path.parent
    )

    values = github_settings.model_dump()
    values["task_claim_ledger_path"] = github_settings.audit_log_path
    with pytest.raises(ValidationError, match="must be distinct"):
        Settings(**values)

    other = tmp_path / "other-audit"
    other.mkdir(mode=0o700)
    values["task_claim_ledger_path"] = other / "task_claims.ndjson"
    with pytest.raises(ValidationError, match="AUDIT_LOG_PATH directory"):
        Settings(**values)


def test_mcp_path_rejects_dot_segments(tmp_path: Path, snapshot: Path):
    with pytest.raises(ValidationError, match="dot segments"):
        make_github_settings(tmp_path, snapshot, mcp_path="/mcp/../admin")


def test_development_http_public_url_is_loopback_only(
    tmp_path: Path,
    snapshot: Path,
):
    with pytest.raises(ValidationError, match="only on loopback"):
        make_github_settings(
            tmp_path,
            snapshot,
            public_base_url="http://gateway.example.test",
        )


def test_secret_symlink_is_rejected(
    tmp_path: Path,
    snapshot: Path,
):
    settings = make_github_settings(tmp_path, snapshot)
    target = settings.gateway_secrets_dir / "real-token"
    target.write_text("github_pat_symlink_target_value_12345", encoding="utf-8")
    target.chmod(0o600)
    linked = settings.gateway_secrets_dir / "linked-token"
    linked.symlink_to(target)
    with pytest.raises(ValidationError, match="opened safely"):
        make_github_settings(
            tmp_path,
            snapshot,
            github_collaborator_token_file=linked,
        )


def test_loaded_config_error_does_not_echo_absolute_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    missing = tmp_path / "private" / "missing-snapshot"
    monkeypatch.setenv("AUTH_MODE", "authless_local")
    monkeypatch.setenv("API_HOST", "127.0.0.1")
    monkeypatch.setenv("PUBLIC_BASE_URL", "")
    monkeypatch.setenv("JN_SNAPSHOT_PATH", str(missing))
    with pytest.raises(ConfigError) as raised:
        Settings.load(tmp_path / "does-not-exist.env")
    assert str(missing) not in str(raised.value)


def test_process_entrypoint_honors_validated_listener(
    snapshot: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    settings = Settings(
        auth_mode="authless_local",
        api_host="127.0.0.1",
        api_port=18999,
        public_base_url="",
        jn_snapshot_path=snapshot,
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr("app.run.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.run.uvicorn.run",
        lambda target, **kwargs: captured.update(target=target, **kwargs),
    )
    run_main()
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 18999
    assert captured["access_log"] is False
