"""Prove the server credential can check one known real collaborator."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from app.config import read_private_text_secret
from app.mcp.github_oauth import CollaboratorState, ContributorAuthorizer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Call GitHub's fixed JN Engine collaborator endpoint. "
            "The named login must be a known real collaborator."
        )
    )
    parser.add_argument("--login", required=True)
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path("/secrets/github_collaborator_token"),
    )
    return parser


async def run_preflight(*, login: str, token_file: Path) -> int:
    token = read_private_text_secret(
        token_file,
        label="GitHub collaborator token",
        minimum_bytes=20,
    )
    authorizer = ContributorAuthorizer(server_token=token)
    try:
        decision = await authorizer.preflight(login=login)
    finally:
        await authorizer.aclose()

    status_text = (
        str(decision.status_code)
        if decision.status_code is not None
        else decision.reason
    )
    print(f"collaborator credential preflight: login={login} status={status_text}")
    if (
        decision.state is CollaboratorState.ALLOWED
        and decision.status_code == 204
    ):
        return 0
    return 2


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(
            run_preflight(login=args.login, token_file=args.token_file)
        )
    except ValueError as exc:
        print(f"collaborator credential preflight failed: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
