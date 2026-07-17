# Security Model

The implementation and tests enforce:

- GitHub login alone never grants access.
- The collaborator endpoint is fixed to alexscott2718-gif/jn-engine.
- Repository/ref environment values are manifest assertions only.
- HTTP 204 allows, 404 denies, and every other response or network error fails
  closed without being cached.
- Positive decisions live at most 300 seconds by default; negative decisions live
  60 seconds.
- REST and MCP consume one shared typed decision. REST preserves 401/403/503; MCP
  collapses non-allowed states into its normal invalid-token boundary.
- Contributor OAuth requests only read:user. The separate server credential needs
  Metadata: read and an owner or app with repository push access.
- Live CI inspection uses a second credential that is incapable of repository writes:
  Actions: read, Contents: read, and Metadata: read on only
  alexscott2718-gif/jn-engine. It is never reused for collaborator authorization or
  the pull-request write path.
- The pull-request write path uses a third, dedicated credential scoped to only
  alexscott2718-gif/jn-engine with Contents: write and Pull requests: write. It is
  loaded only when ENABLE_WRITE_ACTIONS=true, is never reused for authorization or
  CI reads, and the only ref the code can write is a validated refs/heads/contrib/
  ref — never refs/heads/master, which branch protection independently enforces.
- OAuth proxy state is encrypted and confined to the mode-0700 secrets mount.
- Secrets are mode-0600 files and are never accepted in URLs.
- The shell flag fails startup when true. The write flag enables only the audited
  claim_task, release_task, and PR-only open_pr tools; it requires the engine profile,
  an authenticated deployment, and the mounted mode-0600 pull-request credential,
  and it fails startup otherwise. Claims never receive or use that credential. With
  the flag off, all three tools remain registered but fail closed with write_disabled.
- Every live status, claim, release, and open_pr call is appended and fsynced as sanitized
  NDJSON on the dedicated audit mount. Proposed file contents, the bearer, and the
  outbound GitHub credentials are never record fields; an unavailable audit sink
  fails the call.
- Claim ownership is derived only from the authenticated principal. Claim decisions
  and their audit event are one locked append to a dedicated mode-0600 versioned
  ledger, distinct from the rotatable status/PR audit log; expiry is 15 minutes
  through 24 hours, idempotency replays the same claim, competing callers conflict,
  and release requires both ownership and the opaque claim ID. Malformed, partial,
  oversized, linked, unsupported-version, or incorrectly permissioned ledgers fail
  closed and use the documented drain-and-archive recovery procedure.
- open_pr validates every input before any network use: a contrib/ branch allowlist,
  bounded printable title/body, bounded file counts and sizes, and repository-relative
  paths with no traversal, no .git/, and no workflow writes. Idempotency keys make
  retries return the existing pull request; a key mismatch on an existing branch is a
  typed conflict, and mutations are never retried after they may have applied. An
  optional expected base commit rejects stale snapshot-derived full-file writes before
  mutation while preserving replay of an already-created idempotent branch.
- Ground-truth requests compose fetch, live status, and open_pr against the fixed
  `docs/ground_truth_requests.md`; they add no tool or credential. The engine document
  is append-only, the fetched and live commits must match, and open_pr enforces that
  same expected base commit before creating a branch.
- The engine service and optional gateway-development service each load exactly one
  fully validated, non-writable snapshot and never switch it in-process.
- Content IDs bind repository-relative paths to the active commit; stale IDs fail.
  Engine IDs use `jn1_`; gateway-repository IDs use `jng1_`, so IDs cannot cross
  corpus boundaries.
- Every path is inventory-backed, contained, non-symlink, regular, and size bounded
  before use. Hidden paths are rejected except the gateway corpus's exact reviewed
  root templates and `.github/workflows` allowlist.
- Search is literal and case-insensitive. Ripgrep receives a fixed argv list with the
  query after `-e` and `--`; `shell=False`; Python is the byte-equivalent fallback.
- Ripgrep is the only application subprocess. Task, context, and symbol cores have no
  HTTP client or code-execution path.

The service has no application-level rate limiter. Bounded requests, timeouts, and
collaborator caching constrain local and upstream work; operators must apply suitable
limits at their HTTPS proxy or edge. The GitHub read client retries bounded 5xx and
explicit rate-limit responses with backoff, then fails the entire call with
`upstream_unavailable`; it never returns partial status. Exact production thresholds
and infrastructure inventory are operator-private configuration.

Each container runs non-root with a read-only root filesystem, one read-only content
mount, bounded tmpfs, no capabilities, no-new-privileges, no Docker socket, and no Git
executable. The engine service has its own writable secrets and persistent audit
mounts. The gateway-development service has a different writable secrets mount and
no audit mount because all three of its tools are closed-world reads.

The data plane, route/tool contracts, Python 3.11 suite, device fallback, real GitHub
OAuth flow, collaborator-token preflight, public edge policy, and hardened image
inspection are complete. Live non-collaborator and collaborator-removal exercises
still require a separate GitHub test identity; automated fail-closed coverage remains
mandatory until that external proof is available.
