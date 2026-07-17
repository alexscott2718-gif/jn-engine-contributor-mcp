# Gateway Repository MCP

## Purpose

Account-level GitHub connection in a chat client does not guarantee that repository
tools are callable in a particular conversation. The dedicated source-profile MCP
closes that gap without granting broad GitHub or host access.

Connector URL after deployment:

~~~text
https://source-mcp.example.org/mcp
~~~

It reuses the reviewed GitHub contributor-policy implementation, but runs with its own
OAuth client, signing key, encrypted state directory, process, hostname, exact token
audience, immutable snapshot, and content-ID namespace. Sharing one OAuth provider
across two MCP paths is deliberately forbidden because FastMCP binds tokens to one
resource URL.

## Tools

1. `repository_context` — bounded orientation from the README, intended-usage guide,
   MCP surface, security model, deployment guide, and public repository boundary.
2. `search` — literal case-insensitive search with `source`, `docs`, `tests`, and
   `deploy` scopes.
3. `fetch` — bounded retrieval by a commit-bound `jng1_` ID from search.

All three tools are read-only, non-destructive, idempotent, and closed-world. Search
must precede fetch after every snapshot refresh.

## Corpus Boundary

Included: root Docker/Compose and configuration templates, `app/`, `tests/`,
`deploy/`, `scripts/`, `docs/`, `cloudflared/`, and `.github/workflows/`.

Excluded: `.git`, live `.env`, credentials, OAuth state, audit records, `dist/`,
virtual environments, caches, package build output, binary assets, symlinks, hard
links, oversized files, and every path outside the fixed allowlist.

The MCP snapshots `refs/heads/main`; it never serves the mutable working tree and
never runs Git inside the application container.

## Deployment Gate

Before selection:

1. Copy `.env.gateway-repo.example` to ignored `.env.gateway-repo` and set the
   dedicated public origin and loopback port.
2. Create a separate GitHub OAuth app for that origin. Store its client secret,
   collaborator token, and a new JWT signing key in a separate mode-0700 secrets
   directory containing only mode-0600 files.
3. Add the dedicated hostname to the selected HTTPS proxy or tunnel and apply a
   bounded OAuth/MCP edge policy.
4. Keep the engine and gateway-repository OAuth state directories separate. Never
   point both services at the same secrets mount.

~~~sh
GATEWAY_REPO_MIRROR_PATH=/ops/jn-engine-contributor-mcp.git \
GATEWAY_REPO_SNAPSHOT_ROOT=/ops/jn-gateway-snapshots \
deploy/refresh_gateway_snapshot.sh
~~~

The command exports and validates the snapshot, makes it non-writable, selects it
atomically, starts the reviewed standalone Compose service, checks the active commit
through its loopback health endpoint, and retains the newest three healthy snapshots.
Register the new URL as a separate custom MCP in ChatGPT and Claude App only after
this gate passes. Run the MCP Inspector OAuth flow before app registration.

## Intended Contributor Workflow

1. The operator promotes a reviewed commit into the immutable snapshot. The MCP never
   reads an operator working tree and contributors cannot force a refresh.
2. Each contributor connects the dedicated MCP URL from their own ChatGPT, Claude, or
   MCP-capable coding client and completes GitHub OAuth individually. Do not share a
   bearer token, browser session, OAuth state directory, or service secret.
3. The agent calls `repository_context`, then a fresh `search`, then `fetch` for the
   exact files it needs. A snapshot refresh invalidates old `jng1_` IDs by design.
4. The MCP supplies grounded read-only repository context. It does not edit files,
   create branches, or open pull requests. Contributors make changes in their own
   checkout or coding workspace and submit them through the repository's normal
   review path.
5. The operator reviews and merges contributions, then promotes another immutable
   snapshot. That makes the newly merged behavior available to every connected agent
   without exposing mutable server state.

Authorization is intentionally not anonymous: the service admits identities that
GitHub reports as collaborators on the fixed JN Engine repository. GitHub, the MCP
operator, and the contributor's AI platform can therefore associate activity with a
GitHub identity. The dedicated service does not add an application audit log or log
tool arguments, but reverse-proxy, DNS, GitHub, and AI-platform metadata may still
exist outside the application.

For pseudonymous public collaboration, use a project GitHub identity or organization
that contains no personal name, avatar, biography, location, personal links, or
public email; use GitHub's no-reply commit address and inspect commit history before
promotion. Keep contributor accounts separate from operator credentials, minimize
proxy and host log retention, and never place private contact details or credentials
in admitted files. These measures reduce public linkage but do not make the operator
anonymous to GitHub, the AI provider, network providers, or legal process.

## Known Separate Follow-up

The current JN Engine snapshot policy predates the portable Docker/CI campaign and
does not admit the engine repository's root `Dockerfile`, `CONTRIBUTING.md`,
`.devcontainer`, or `.github/workflows`. That is why a client grounded only through
the existing engine MCP can incorrectly report that those files do not exist. Fixing
the engine corpus allowlist and refreshing its immutable snapshot is a separate
compatibility change; this gateway-development MCP does not disguise or infer those
missing engine files.
