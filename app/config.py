"""Environment parsing and fail-fast safety validation."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)

APP_NAME = "jn-engine-contributor-mcp"
EXPECTED_REPOSITORY = "alexscott2718-gif/jn-engine"
EXPECTED_REF = "refs/heads/master"
EXPECTED_GATEWAY_REPOSITORY = "alexscott2718-gif/jn-engine-contributor-mcp"
EXPECTED_GATEWAY_REF = "refs/heads/main"
GITHUB_API_VERSION = "2026-03-10"
GITHUB_API_BASE_URL = "https://api.github.com"
GITHUB_OAUTH_SCOPE = "read:user"
GITHUB_OAUTH_CALLBACK_PATH = "/auth/callback"

AuthMode = Literal["github", "device", "authless_local"]
AppEnvironment = Literal["development", "production"]
SearchEngine = Literal["auto", "ripgrep", "python"]
ServiceProfile = Literal["engine", "gateway_repository"]

_LOOPBACK_WILDCARD = re.compile(
    r"^http://(?:localhost|127\.0\.0\.1|\[::1\]):\*(?:/[^\s?#]*)?$",
    re.IGNORECASE,
)


class ConfigError(RuntimeError):
    """Raised when the service cannot start safely."""


def _as_bool(name: str, value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def _as_redirect_list(value: str) -> tuple[str, ...]:
    if not value.strip():
        return ()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            "OAUTH_ALLOWED_CLIENT_REDIRECT_URIS must be a JSON array"
        ) from exc
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ConfigError(
            "OAUTH_ALLOWED_CLIENT_REDIRECT_URIS must be a JSON string array"
        )
    return tuple(parsed)


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_public_base_url(value: str, *, production: bool) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("PUBLIC_BASE_URL is malformed") from exc
    if (
        not parsed.scheme
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(
            "PUBLIC_BASE_URL must be an origin without credentials, path, query, or fragment"
        )
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("PUBLIC_BASE_URL port is invalid")
    if production and parsed.scheme != "https":
        raise ValueError("PUBLIC_BASE_URL must use HTTPS in production")
    if not production and parsed.scheme not in {"http", "https"}:
        raise ValueError("PUBLIC_BASE_URL must use HTTP or HTTPS")
    if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
        raise ValueError("HTTP PUBLIC_BASE_URL is allowed only on loopback")


def _validate_redirect_pattern(value: str) -> None:
    if not value or value != value.strip():
        raise ValueError("OAuth redirect patterns must be nonempty and trimmed")
    if _LOOPBACK_WILDCARD.fullmatch(value):
        return
    if "*" in value:
        raise ValueError(
            "OAuth redirect wildcards are allowed only as a loopback HTTP port wildcard"
        )
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid OAuth redirect URI: {value!r}") from exc
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"unsafe OAuth redirect URI: {value!r}")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"invalid OAuth redirect port: {value!r}")
    if _is_loopback(parsed.hostname):
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"loopback OAuth redirects must use HTTP or HTTPS: {value!r}")
    elif parsed.scheme != "https":
        raise ValueError(f"non-loopback OAuth redirects must use HTTPS: {value!r}")


def _validate_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError("GATEWAY_SECRETS_DIR is missing or inaccessible") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("GATEWAY_SECRETS_DIR must be a real directory")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValueError("GATEWAY_SECRETS_DIR must have mode 0700")
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        raise ValueError("GATEWAY_SECRETS_DIR must be readable and writable")


def read_private_secret_bytes(
    path: Path,
    *,
    label: str,
    minimum_bytes: int,
) -> bytes:
    """Read one non-symlink mode-0600 secret without exposing its value."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"{label} file cannot be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} must be a regular non-symlink file")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError(f"{label} file must have mode 0600")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            value = handle.read(16_385)
    finally:
        os.close(descriptor)
    if len(value) > 16_384:
        raise ValueError(f"{label} file is unexpectedly large")
    if value.endswith(b"\n"):
        value = value[:-1]
        if value.endswith(b"\r"):
            value = value[:-1]
    if len(value) < minimum_bytes:
        raise ValueError(f"{label} is missing or too short")
    return value


def read_private_text_secret(
    path: Path,
    *,
    label: str,
    minimum_bytes: int,
) -> str:
    raw = read_private_secret_bytes(
        path,
        label=label,
        minimum_bytes=minimum_bytes,
    )
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} must be UTF-8 text") from exc
    if value != value.strip() or any(character.isspace() for character in value):
        raise ValueError(f"{label} must not contain whitespace")
    return value


class Settings(BaseModel):
    """Frozen process settings; repository identity is code-owned."""

    model_config = ConfigDict(frozen=True)

    app_name: str = APP_NAME
    app_env: AppEnvironment = "development"
    api_host: str = "0.0.0.0"
    api_port: int = 8788
    public_base_url: str = ""
    mcp_path: str = "/mcp"
    service_profile: ServiceProfile = "engine"
    auth_mode: AuthMode = "github"
    jn_snapshot_path: Path = Path("/data/jn-engine")
    gateway_repo_snapshot_path: Path = Path("/data/jn-engine-contributor-mcp")
    expected_repository: str = EXPECTED_REPOSITORY
    expected_ref: str = EXPECTED_REF
    search_engine: SearchEngine = "auto"
    gateway_secrets_dir: Path = Path("/secrets")
    github_oauth_client_id: str = ""
    github_oauth_client_secret_file: Path = Path(
        "/secrets/github_oauth_client_secret"
    )
    github_collaborator_token_file: Path = Path(
        "/secrets/github_collaborator_token"
    )
    github_actions_read_token_file: Path = Path(
        "/secrets/github_actions_read_token"
    )
    audit_log_path: Path = Path("/audit/tool_calls.ndjson")
    oauth_jwt_signing_key_file: Path = Path("/secrets/oauth_jwt_signing_key")
    oauth_allowed_client_redirect_uris: tuple[str, ...] = ()
    github_collab_cache_ttl_seconds: int = 300
    github_collab_negative_ttl_seconds: int = 60
    mcp_enrollment_secret_file: Path | None = None
    enable_write_actions: bool = False
    enable_shell_actions: bool = False
    log_level: str = "INFO"

    @field_validator("mcp_path")
    @classmethod
    def validate_mcp_path(cls, value: str) -> str:
        segments = value.split("/")
        if (
            not value.startswith("/")
            or value == "/"
            or "//" in value
            or re.fullmatch(r"/[A-Za-z0-9/_-]+", value) is None
            or any(segment in {".", ".."} for segment in segments)
        ):
            raise ValueError("MCP_PATH must be a non-root absolute path without dot segments")
        return value.rstrip("/")

    @field_validator("oauth_allowed_client_redirect_uris")
    @classmethod
    def validate_redirects(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        unique = tuple(dict.fromkeys(values))
        for value in unique:
            _validate_redirect_pattern(value)
        return unique

    @model_validator(mode="after")
    def validate_safety(self) -> "Settings":
        if not self.app_name.strip():
            raise ValueError("APP_NAME must not be empty")
        if self.app_env == "production" and self.app_name != APP_NAME:
            raise ValueError(f"APP_NAME must be {APP_NAME!r} in production")
        if not self.api_host.strip():
            raise ValueError("API_HOST must not be empty")
        if not 1 <= self.api_port <= 65535:
            raise ValueError("API_PORT must be in the range 1..65535")
        if self.expected_repository != EXPECTED_REPOSITORY:
            raise ValueError(
                "EXPECTED_REPOSITORY is a fixed manifest assertion and cannot redirect the service"
            )
        if self.expected_ref != EXPECTED_REF:
            raise ValueError(
                "EXPECTED_REF is a fixed manifest assertion and cannot redirect the service"
            )
        if self.enable_write_actions:
            raise ValueError("ENABLE_WRITE_ACTIONS=true is forbidden in the read-only MVP")
        if self.enable_shell_actions:
            raise ValueError("ENABLE_SHELL_ACTIONS=true is forbidden in the read-only MVP")
        if not 30 <= self.github_collab_cache_ttl_seconds <= 900:
            raise ValueError("GITHUB_COLLAB_CACHE_TTL_SECONDS must be in 30..900")
        if not 15 <= self.github_collab_negative_ttl_seconds <= 300:
            raise ValueError("GITHUB_COLLAB_NEGATIVE_TTL_SECONDS must be in 15..300")
        if (
            self.github_collab_negative_ttl_seconds
            > self.github_collab_cache_ttl_seconds
        ):
            raise ValueError("negative collaborator TTL cannot exceed the positive TTL")
        normalized_log_level = self.log_level.upper()
        if normalized_log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError("LOG_LEVEL is invalid")
        if self.app_env == "production" and normalized_log_level == "DEBUG":
            raise ValueError("LOG_LEVEL=DEBUG is forbidden in production")

        if self.service_profile == "engine":
            if not self.jn_snapshot_path.is_dir():
                raise ValueError("JN_SNAPSHOT_PATH is missing or not a directory")
            if not (self.jn_snapshot_path / "manifest.json").is_file():
                raise ValueError("JN_SNAPSHOT_PATH/manifest.json is missing")
            if not (self.jn_snapshot_path / "content").is_dir():
                raise ValueError("JN_SNAPSHOT_PATH/content is missing")
        else:
            if not self.gateway_repo_snapshot_path.is_dir():
                raise ValueError(
                    "GATEWAY_REPO_SNAPSHOT_PATH is missing or not a directory"
                )
            if not (self.gateway_repo_snapshot_path / "manifest.json").is_file():
                raise ValueError(
                    "GATEWAY_REPO_SNAPSHOT_PATH/manifest.json is missing"
                )
            if not (self.gateway_repo_snapshot_path / "content").is_dir():
                raise ValueError(
                    "GATEWAY_REPO_SNAPSHOT_PATH/content is missing"
                )
        if self.search_engine == "ripgrep" and shutil.which("rg") is None:
            raise ValueError("SEARCH_ENGINE=ripgrep requires rg")

        production = self.app_env == "production"
        if self.public_base_url:
            _validate_public_base_url(self.public_base_url, production=production)
        elif production:
            raise ValueError("PUBLIC_BASE_URL is required in production")

        if self.auth_mode == "authless_local":
            if self.public_base_url:
                raise ValueError("AUTH_MODE=authless_local forbids PUBLIC_BASE_URL")
            if not _is_loopback(self.api_host):
                raise ValueError("AUTH_MODE=authless_local requires a loopback API_HOST")
            return self

        if not self.public_base_url:
            raise ValueError(f"AUTH_MODE={self.auth_mode} requires PUBLIC_BASE_URL")
        _validate_private_directory(self.gateway_secrets_dir)
        if production and not self.oauth_allowed_client_redirect_uris:
            raise ValueError(
                "OAUTH_ALLOWED_CLIENT_REDIRECT_URIS must not be empty in production"
            )

        if self.service_profile == "engine":
            if not self.audit_log_path.is_absolute():
                raise ValueError("AUDIT_LOG_PATH must be absolute")
            audit_parent = self.audit_log_path.parent
            try:
                audit_metadata = audit_parent.lstat()
            except OSError as exc:
                raise ValueError(
                    "AUDIT_LOG_PATH parent is missing or inaccessible"
                ) from exc
            if stat.S_ISLNK(audit_metadata.st_mode) or not stat.S_ISDIR(
                audit_metadata.st_mode
            ):
                raise ValueError("AUDIT_LOG_PATH parent must be a real directory")
            if stat.S_IMODE(audit_metadata.st_mode) != 0o700:
                raise ValueError("AUDIT_LOG_PATH parent must have mode 0700")
            if not os.access(audit_parent, os.R_OK | os.W_OK | os.X_OK):
                raise ValueError(
                    "AUDIT_LOG_PATH parent must be readable and writable"
                )

        secret_paths = [self.oauth_jwt_signing_key_file]
        if self.service_profile == "engine":
            secret_paths.append(self.github_actions_read_token_file)
        if self.auth_mode == "github":
            client_id = self.github_oauth_client_id
            if (
                client_id != client_id.strip()
                or any(character.isspace() for character in client_id)
                or len(client_id) < 8
                or client_id == "change-me"
            ):
                raise ValueError("GITHUB_OAUTH_CLIENT_ID is required in github mode")
            secret_paths.extend(
                [
                    self.github_oauth_client_secret_file,
                    self.github_collaborator_token_file,
                ]
            )
        else:
            if self.mcp_enrollment_secret_file is None:
                raise ValueError("MCP_ENROLLMENT_SECRET_FILE is required in device mode")
            secret_paths.append(self.mcp_enrollment_secret_file)

        secrets_root = self.gateway_secrets_dir.resolve()
        for path in secret_paths:
            if not path.resolve().is_relative_to(secrets_root):
                raise ValueError("secret files must remain inside GATEWAY_SECRETS_DIR")
        if self.auth_mode == "github":
            read_private_text_secret(
                self.github_oauth_client_secret_file,
                label="GitHub OAuth client secret",
                minimum_bytes=16,
            )
            read_private_text_secret(
                self.github_collaborator_token_file,
                label="GitHub collaborator token",
                minimum_bytes=20,
            )
        else:
            enrollment_secret_file = self.mcp_enrollment_secret_file
            if enrollment_secret_file is None:
                raise ValueError("MCP_ENROLLMENT_SECRET_FILE is required in device mode")
            read_private_text_secret(
                enrollment_secret_file,
                label="MCP enrollment secret",
                minimum_bytes=16,
            )
        read_private_secret_bytes(
            self.oauth_jwt_signing_key_file,
            label="OAuth JWT signing key",
            minimum_bytes=32,
        )
        return self

    @property
    def citation_base_url(self) -> str:
        return self.public_base_url.rstrip("/") or f"http://{self.api_host}:{self.api_port}"

    def github_oauth_client_secret(self) -> str:
        return read_private_text_secret(
            self.github_oauth_client_secret_file,
            label="GitHub OAuth client secret",
            minimum_bytes=16,
        )

    def github_collaborator_token(self) -> str:
        return read_private_text_secret(
            self.github_collaborator_token_file,
            label="GitHub collaborator token",
            minimum_bytes=20,
        )

    def github_actions_read_token(self) -> str:
        """Load the separate, read-only Actions/Contents/Metadata credential."""
        return read_private_text_secret(
            self.github_actions_read_token_file,
            label="GitHub Actions read token",
            minimum_bytes=20,
        )

    def oauth_jwt_signing_key(self) -> bytes:
        return read_private_secret_bytes(
            self.oauth_jwt_signing_key_file,
            label="OAuth JWT signing key",
            minimum_bytes=32,
        )

    def mcp_enrollment_secret(self) -> str:
        if self.mcp_enrollment_secret_file is None:
            raise ValueError("MCP enrollment secret is unavailable outside device mode")
        return read_private_text_secret(
            self.mcp_enrollment_secret_file,
            label="MCP enrollment secret",
            minimum_bytes=16,
        )

    @classmethod
    def load(cls, env_file: str | Path = ".env") -> "Settings":
        file_values = {
            key: value
            for key, value in dotenv_values(env_file).items()
            if value is not None
        }

        def value(name: str, default: str = "") -> str:
            return os.environ.get(name, file_values.get(name, default))

        enrollment_path = value("MCP_ENROLLMENT_SECRET_FILE")
        try:
            return cls(
                app_name=value("APP_NAME", APP_NAME),
                app_env=value("APP_ENV", "development"),
                api_host=value("API_HOST", "0.0.0.0"),
                api_port=int(value("API_PORT", "8788")),
                public_base_url=value("PUBLIC_BASE_URL"),
                mcp_path=value("MCP_PATH", "/mcp"),
                service_profile=value("SERVICE_PROFILE", "engine"),
                auth_mode=value("AUTH_MODE", "github"),
                jn_snapshot_path=Path(value("JN_SNAPSHOT_PATH", "/data/jn-engine")),
                gateway_repo_snapshot_path=Path(
                    value(
                        "GATEWAY_REPO_SNAPSHOT_PATH",
                        "/data/jn-engine-contributor-mcp",
                    )
                ),
                expected_repository=value("EXPECTED_REPOSITORY", EXPECTED_REPOSITORY),
                expected_ref=value("EXPECTED_REF", EXPECTED_REF),
                search_engine=value("SEARCH_ENGINE", "auto"),
                gateway_secrets_dir=Path(
                    value("GATEWAY_SECRETS_DIR", "/secrets")
                ),
                github_oauth_client_id=value("GITHUB_OAUTH_CLIENT_ID"),
                github_oauth_client_secret_file=Path(
                    value(
                        "GITHUB_OAUTH_CLIENT_SECRET_FILE",
                        "/secrets/github_oauth_client_secret",
                    )
                ),
                github_collaborator_token_file=Path(
                    value(
                        "GITHUB_COLLABORATOR_TOKEN_FILE",
                        "/secrets/github_collaborator_token",
                    )
                ),
                github_actions_read_token_file=Path(
                    value(
                        "GITHUB_ACTIONS_READ_TOKEN_FILE",
                        "/secrets/github_actions_read_token",
                    )
                ),
                audit_log_path=Path(
                    value("AUDIT_LOG_PATH", "/audit/tool_calls.ndjson")
                ),
                oauth_jwt_signing_key_file=Path(
                    value(
                        "OAUTH_JWT_SIGNING_KEY_FILE",
                        "/secrets/oauth_jwt_signing_key",
                    )
                ),
                oauth_allowed_client_redirect_uris=_as_redirect_list(
                    value("OAUTH_ALLOWED_CLIENT_REDIRECT_URIS")
                ),
                github_collab_cache_ttl_seconds=int(
                    value("GITHUB_COLLAB_CACHE_TTL_SECONDS", "300")
                ),
                github_collab_negative_ttl_seconds=int(
                    value("GITHUB_COLLAB_NEGATIVE_TTL_SECONDS", "60")
                ),
                mcp_enrollment_secret_file=(
                    Path(enrollment_path) if enrollment_path else None
                ),
                enable_write_actions=_as_bool(
                    "ENABLE_WRITE_ACTIONS", value("ENABLE_WRITE_ACTIONS", "false")
                ),
                enable_shell_actions=_as_bool(
                    "ENABLE_SHELL_ACTIONS", value("ENABLE_SHELL_ACTIONS", "false")
                ),
                log_level=value("LOG_LEVEL", "INFO"),
            )
        except ValidationError as exc:
            details = "; ".join(
                str(error["msg"])
                for error in exc.errors(include_url=False, include_input=False)
            )
            raise ConfigError(
                f"invalid {APP_NAME} configuration: {details}"
            ) from exc
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"invalid {APP_NAME} configuration: {exc}"
            ) from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.load()
