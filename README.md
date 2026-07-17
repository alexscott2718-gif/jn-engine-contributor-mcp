# JN Engine Contributor MCP

A bounded REST and Streamable HTTP MCP gateway for contributors working on
[JN Engine](https://github.com/alexscott2718-gif/jn-engine). It exposes a reviewed,
immutable repository snapshot to ChatGPT, Claude, MCP-compatible coding agents, and
ordinary HTTP clients without giving those clients shell or Git access to the host.

This is the public, contributor-safe distribution. It contains the complete service,
tests, snapshot exporters, Docker configuration, and example deployment files. It
does not contain production credentials, personal paths, private hostnames, audit
records, incident reports, or mutable operator data.

## What It Provides

The engine profile exposes nine tools:

- `search` and `fetch` for commit-bound source and documentation retrieval;
- `list_tasks` and `project_context` for bounded project orientation;
- `claim_task` and `release_task` for audited, expiring task ownership;
- `lookup_symbol` for recovered symbol/address lookup; and
- `check_status` for fail-closed `core` and `assets` GitHub Actions status;
- `open_pr` for a narrowly constrained contributor pull-request path.

An optional source profile exposes this repository itself through `search`, `fetch`,
and `repository_context`. That profile lets a web or app-based agent inspect the MCP
implementation even when the platform's account-level GitHub integration is not
available as a callable chat tool.

Both profiles use immutable snapshots. Search IDs include the active commit and become
invalid after a snapshot refresh, so clients must search again rather than silently
mixing content from different revisions.

## Why Sanitization Does Not Reduce Contributor Value

The useful parts are present: application behavior, interfaces, tests, fixture data,
dependency locks, container boundaries, deployment examples, and contribution rules.
Only environment-specific material was excluded. Contributors can:

- run all unit and integration tests;
- build and inspect the container;
- create snapshots from the public JN Engine repository;
- exercise either MCP profile locally without authentication;
- configure their own GitHub OAuth deployment;
- develop new bounded tools and submit pull requests; and
- use the source profile to ground ChatGPT or Claude in this implementation.

See [Intended Usage](docs/intended_usage.md) for contributor and operator workflows,
and [Public Repository Boundary](docs/public_repository_boundary.md) for the exact
public/private split.

## Quick Start

Requirements: Python 3.11 through 3.13, Git, and `ripgrep`.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --constraint requirements.lock -e '.[test]'
```

Build a local immutable snapshot of JN Engine:

```sh
JN_REPO_MIRROR_PATH="$PWD/.local/jn-engine.git" \
JN_SNAPSHOT_ROOT="$PWD/.local/snapshots" \
deploy/refresh_snapshot.sh --build-only
```

The command prints the promoted snapshot path. Start a loopback-only development
server with that path:

```sh
APP_ENV=development \
AUTH_MODE=authless_local \
API_HOST=127.0.0.1 \
JN_SNAPSHOT_PATH="$PWD/.local/snapshots/<commit>" \
.venv/bin/python -m app.run
```

Connect an MCP client to `http://127.0.0.1:8788/mcp`. Authless mode is deliberately
restricted to a loopback listener and is not a production configuration.

## Verification

CI constructs its own immutable JN Engine fixture before running the suite. Locally,
set `JN_TEST_SNAPSHOT_PATH` to the snapshot created above:

```sh
JN_TEST_SNAPSHOT_PATH="$PWD/.local/snapshots/<commit>" \
  .venv/bin/python -m pytest -q
```

Production GitHub OAuth configuration and secret-file permissions are documented in
[Deployment](docs/deployment.md). MCP contracts are in
[MCP Surface](docs/mcp_surface.md).

## Security Properties

- Repository identity and ref are code-owned and cannot be redirected by environment
  variables.
- Snapshot files are inventory checked, contained, non-symlink, regular, bounded, and
  read-only in production.
- OAuth state is encrypted before being stored in a mode-0700 file-tree directory.
- Secret values are accepted through mode-0600 files, not command-line arguments or
  URLs.
- Write actions require the authenticated engine profile and a dedicated PR token;
  shell actions always fail startup when enabled.
- The container runs non-root with a read-only root filesystem, no capabilities, no
  Docker socket, and no Git executable.

Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## License

Licensed under the MIT License. See [LICENSE](LICENSE).
