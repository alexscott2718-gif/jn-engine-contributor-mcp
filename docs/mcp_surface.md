# MCP Surface

The production Streamable HTTP server registers exactly these seven tools:

1. search
2. fetch
3. list_tasks
4. project_context
5. lookup_symbol
6. check_status
7. open_pr

Six tools are read-only; every tool is non-destructive, idempotent, and bounded. The
five snapshot tools are closed-world and share the REST core. `check_status` is an
open-world read against only the fixed JN Engine GitHub repository. `open_pr` is the
single write tool: it proposes work as a pull request and can never push to protected
`master`. No resources or prompts are registered.

| Tool | Inputs |
|---|---|
| `search` | `query`, `scope=all|source|docs|re|tasks`, `limit=1..50` |
| `fetch` | commit-bound `id` returned by search |
| `list_tasks` | `status`, `source`, `limit=1..100` |
| `project_context` | `max_chars=1000..20000` |
| `lookup_symbol` | one or more of `name`, `address`, `class_name`, `fourcc`; `limit=1..50` |
| `check_status` | exactly one of `pr` or `branch`; optional full `commit` SHA |
| `open_pr` | `branch` (contrib/ allowlist), `title`, `files` (path+content), `idempotency_key`; optional `body` |

Search returns only `id`, `title`, and a commit-pinned URL. Fetch returns bounded text
and exact snapshot metadata. A stale fetch ID raises a tool error instructing the
caller to search again.

## Live `check_status` Contract

`check_status` reads GitHub Actions state through a separate least-privilege credential
that has no repository write permission. Callers cannot select another repository.
`check_status` and `open_pr` have `openWorldHint: true`; the five snapshot tools have
`openWorldHint: false`. Every tool except `open_pr` has `readOnlyHint: true`; all
seven retain `destructiveHint: false` and `idempotentHint: true`.

`check_status` always reports both required contexts. An unreported context has state
`missing`, makes `overall` equal `blocked`, and names the absent context in
`blocked_reason`; absence can never become a successful result. Failures are short
JSON tool errors with one of `credential_unavailable`, `upstream_unavailable`,
`bad_args`, or `not_found`. Every invocation appends a durable audit record containing
the caller identity, sanitized arguments, resolved commit, outcome, and ordered GitHub
status trail. Bearer and GitHub token values are never serialized.

## Live `open_pr` Contract

`open_pr` writes through a third, dedicated credential that can create branches,
commits, and pull requests on only alexscott2718-gif/jn-engine. It is never the
collaborator-authorization credential and never the Actions read credential.

The tool creates one commit on a `contrib/<name>` branch from the current `master`
head using the provided UTF-8 files (1..32 files, 200k characters per file, 1M
combined; repository-relative paths with no dot segments, no `.git/`, and no
`.github/workflows/` writes), then opens one non-draft pull request with base
`master`. The only ref it ever creates is the validated `refs/heads/contrib/...`
ref, and pull requests cannot merge without the protected `core` and `assets`
contexts, so a direct write to `master` is structurally impossible in the tool and
independently blocked by branch protection.

The required `idempotency_key` (8..64 characters) is recorded as a commit trailer
with the caller identity. Replaying a call whose branch head carries the same key
returns the same pull request with `replayed: true` and creates nothing new; a
branch that exists with a different key is a `conflict`. Mutating requests are sent
exactly once — after a mutation may have been applied, any transport error or
unexpected status fails the whole call closed with no retry and no partial result.

When the deployment has not set `ENABLE_WRITE_ACTIONS=true` with the dedicated
credential mounted, the registered tool fails closed with `write_disabled`.
Failures are short JSON tool errors with one of `credential_unavailable`,
`upstream_unavailable`, `bad_args`, `not_found`, `conflict`, or `write_disabled`.
Every invocation appends a durable audit record containing the caller identity,
sanitized arguments (file paths and sizes, never file contents), resolved base and
head commits, pull-request number, outcome, and ordered GitHub status trail. Bearer
and GitHub token values are never serialized.

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
