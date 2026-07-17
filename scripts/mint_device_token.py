#!/usr/bin/env python3
"""Mint one named, gateway-signed token for a headless device."""

from __future__ import annotations

import argparse
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from authlib.jose import JsonWebToken
from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.mcp.device_oauth import JN_READ_SCOPE, load_signing_key  # noqa: E402

_DEVICE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")


def _env_value(document: dict[str, str | None], name: str, default: str = "") -> str:
    value = document.get(name)
    return value if value is not None else default


def _validate_issuer(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("PUBLIC_BASE_URL must be an HTTPS origin")
    return value.rstrip("/")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--client-id",
        required=True,
        help="stable device/agent name recorded in the token principal",
    )
    parser.add_argument(
        "--ttl-days",
        type=float,
        default=30.0,
        help="token lifetime in days, greater than zero and at most 365",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(__file__).resolve().parent.parent / ".env",
        help="deployment environment file",
    )
    parser.add_argument(
        "--key-file",
        type=Path,
        help=(
            "host signing-key path; defaults to "
            "GATEWAY_SECRETS_HOST_PATH/oauth_jwt_signing_key"
        ),
    )
    args = parser.parse_args()

    if _DEVICE_IDENTIFIER.fullmatch(args.client_id) is None:
        parser.error(
            "--client-id must be 1..128 safe ASCII characters beginning with alphanumeric"
        )
    if not 0 < args.ttl_days <= 365:
        parser.error("--ttl-days must be greater than zero and at most 365")

    document = dotenv_values(args.env_file)
    try:
        issuer = _validate_issuer(_env_value(document, "PUBLIC_BASE_URL"))
    except ValueError as exc:
        parser.error(str(exc))
    mcp_path = _env_value(document, "MCP_PATH", "/mcp")
    if not mcp_path.startswith("/") or mcp_path == "/":
        parser.error("MCP_PATH must be a non-root absolute path")

    key_file = args.key_file
    if key_file is None:
        secrets_path = _env_value(document, "GATEWAY_SECRETS_HOST_PATH")
        if not secrets_path:
            parser.error(
                "GATEWAY_SECRETS_HOST_PATH is required when --key-file is omitted"
            )
        key_file = Path(secrets_path) / "oauth_jwt_signing_key"

    try:
        key = load_signing_key(key_file)
    except ValueError as exc:
        parser.error(str(exc))

    now = int(time.time())
    expires_at = now + int(args.ttl_days * 86_400)
    claims = {
        "iss": issuer,
        "aud": f"{issuer}{mcp_path.rstrip('/')}",
        "sub": args.client_id,
        "client_id": args.client_id,
        "scope": JN_READ_SCOPE,
        "iat": now,
        "exp": expires_at,
        "jti": secrets.token_urlsafe(16),
        "token_use": "access",
    }
    token = (
        JsonWebToken(["RS256"])
        .encode(
            {"alg": "RS256", "typ": "JWT", "kid": key.kid},
            claims,
            key.private_pem,
        )
        .decode()
    )
    expiry = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()
    print(
        f"device token client_id={args.client_id!r} scope={JN_READ_SCOPE} "
        f"expires={expiry}",
        file=sys.stderr,
    )
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
