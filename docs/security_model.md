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
  alexscott2718-gif/jn-engine. It is never reused for collaborator authorization or a
  future pull-request write path.
- OAuth proxy state is encrypted and confined to the mode-0700 secrets mount.
- Secrets are mode-0600 files and are never accepted in URLs.
- Both write and shell flags fail startup when true.
- Every live status call is appended and fsynced as sanitized NDJSON on the dedicated
  audit mount. The bearer and outbound GitHub credential are never record fields.
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
