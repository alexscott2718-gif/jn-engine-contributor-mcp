# Write Design (implemented as `open_pr`)

The originally deferred write path is now implemented as the single `open_pr` MCP
tool. See `docs/mcp_surface.md` ("Live `open_pr` Contract") and
`docs/security_model.md` for the binding contract: dedicated least-privilege
credential, contrib/-only branch allowlist, path validation, request idempotency,
PR-only enforcement, fail-closed single-attempt mutations, and durable sanitized
audit records. Human review and the protected `core` and `assets` checks remain
mandatory; the served snapshot is never mutated by writes.
