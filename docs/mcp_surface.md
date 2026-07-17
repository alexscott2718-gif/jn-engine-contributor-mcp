# MCP Surface

The production Streamable HTTP server registers exactly these six tools:

1. search
2. fetch
3. list_tasks
4. project_context
5. lookup_symbol
6. check_status

Every live tool is read-only, non-destructive, idempotent, and bounded. The five
snapshot tools are closed-world and share the REST core. `check_status` is an
open-world read against only the fixed JN Engine GitHub repository. No resources or
prompts are registered.

| Tool | Inputs |
|---|---|
| `search` | `query`, `scope=all|source|docs|re|tasks`, `limit=1..50` |
| `fetch` | commit-bound `id` returned by search |
| `list_tasks` | `status`, `source`, `limit=1..100` |
| `project_context` | `max_chars=1000..20000` |
| `lookup_symbol` | one or more of `name`, `address`, `class_name`, `fourcc`; `limit=1..50` |
| `check_status` | exactly one of `pr` or `branch`; optional full `commit` SHA |

Search returns only `id`, `title`, and a commit-pinned URL. Fetch returns bounded text
and exact snapshot metadata. A stale fetch ID raises a tool error instructing the
caller to search again.

## Live `check_status` Contract

`check_status` reads GitHub Actions state through a separate least-privilege credential
that has no repository write permission. Callers cannot select another repository.
The tool has `openWorldHint: true`; the other five tools have `openWorldHint: false`.
All six retain `readOnlyHint: true`, `destructiveHint: false`, and
`idempotentHint: true`.

`check_status` always reports both required contexts. An unreported context has state
`missing`, makes `overall` equal `blocked`, and names the absent context in
`blocked_reason`; absence can never become a successful result. Failures are short
JSON tool errors with one of `credential_unavailable`, `upstream_unavailable`,
`bad_args`, or `not_found`. Every invocation appends a durable audit record containing
the caller identity, sanitized arguments, resolved commit, outcome, and ordered GitHub
status trail. Bearer and GitHub token values are never serialized.

## Gateway Development MCP

When deployed with `SERVICE_PROFILE=gateway_repository`, a dedicated service exposes
this surface at `/mcp` on its own HTTPS origin. It is a distinct connector, process,
OAuth resource audience, state directory, and corpus—not an expansion of the engine
service's `/mcp`.

| Tool | Inputs |
|---|---|
| `search` | `query`, `scope=all|source|docs|tests|deploy`, `limit=1..50` |
| `fetch` | commit-bound `jng1_` ID returned by gateway search |
| `repository_context` | `max_chars=1000..20000` |

The snapshot is fixed to `alexscott2718-gif/jn-engine-contributor-mcp` at
`refs/heads/main`. Its allowlist includes root Docker/Compose/config templates,
application source, tests, deployment scripts, docs, the Cloudflare template, and
`.github/workflows`. It excludes `.git`, `.env`, secrets, audit logs, build output,
virtual environments, caches, and every unapproved path. Apart from public health and
OAuth protocol routes, no REST data routes, resources, prompts, status tools, or write
tools are added for this corpus.
