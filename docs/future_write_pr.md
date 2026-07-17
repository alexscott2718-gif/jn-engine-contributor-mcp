# Write Design (implemented as `open_pr`)

The originally deferred GitHub write path is now implemented as the single
repository-mutating `open_pr` MCP tool. The local `claim_task` and `release_task`
tools modify only the durable ownership ledger. See `docs/mcp_surface.md`
("Live `open_pr` Contract") and
`docs/security_model.md` for the binding contract: dedicated least-privilege
credential, contrib/-only branch allowlist, path validation, request idempotency,
optional expected-base enforcement for snapshot-derived full-file writes, PR-only
enforcement, fail-closed single-attempt mutations, and durable sanitized audit
records. The `request_ground_truth` workflow composes this path rather than adding
another mutation tool. Human review and the protected `core` and `assets` checks
remain mandatory; the served snapshot is never mutated by writes.
