# Deployment

This guide describes the portable deployment contract. It intentionally uses example
hostnames and operator-selected paths; production inventory does not belong in the
public repository.

## Runtime Contract

- Run one application worker with `APP_ENV=production` and `AUTH_MODE=github`.
- Publish the application through HTTPS while keeping the origin listener on loopback.
- Mount one versioned snapshot read-only at `/data/jn-engine`.
- Mount `/secrets` from a mode-0700 host directory owned by container UID/GID 10001.
- Store each credential in a separate mode-0600 file.
- Mount `/audit` from a durable mode-0700 directory for the engine profile.
- Run the container non-root with a read-only root filesystem, dropped capabilities,
  `no-new-privileges`, bounded temporary storage, and no Docker socket.

The optional source profile uses a separate public origin, OAuth application, secrets
directory, state directory, snapshot mount, and Compose service. OAuth tokens are bound
to one resource URL, so the two profiles must not share an OAuth provider or token
audience.

## Configuration

Copy `.env.example` to ignored `.env` and replace every blank/example value. Important
settings include:

- `PUBLIC_BASE_URL=https://mcp.example.org`
- `GITHUB_OAUTH_CLIENT_ID`
- `JN_SNAPSHOT_HOST_PATH`
- `GATEWAY_SECRETS_HOST_PATH`
- `GATEWAY_AUDIT_HOST_PATH`
- `AUDIT_LOG_PATH=/audit/tool_calls.ndjson`
- `TASK_CLAIM_LEDGER_PATH=/audit/task_claims.ndjson`
- a digest-pinned `CLOUDFLARED_IMAGE` if using the optional tunnel profile

Create the secrets directory with mode 0700 and these mode-0600 files:

- `github_oauth_client_secret`
- `github_collaborator_token`
- `github_actions_read_token`
- `oauth_jwt_signing_key`
- `github_pr_write_token` (only when `ENABLE_WRITE_ACTIONS=true`)

The collaborator token checks current access to the fixed JN Engine repository. The
Actions token is separate and should have only Actions read, Contents read, and Metadata
read for that repository. The optional pull-request write token is a third fine-grained
credential for only that repository with Contents write and Pull requests write; it is
required only when `ENABLE_WRITE_ACTIONS=true` and must never be the collaborator or
Actions credential. Generate a new signing key for each deployment; never reuse a
sample value from documentation or tests.

`ENABLE_WRITE_ACTIONS=true` also enables `claim_task` and `release_task` only on the
authenticated engine profile. Those ownership tools never receive or use the GitHub
write credential; their only mutable target is the dedicated durable mode-0600 claim
ledger. The claim ledger must be a different file from the general tool audit log in
the same mode-0700 durable directory.

## Build a Snapshot

The application never runs Git. An operator-side command creates and validates a fixed
repository/ref snapshot:

```sh
JN_REPO_MIRROR_PATH=/srv/jn-mcp/mirrors/jn-engine.git \
JN_SNAPSHOT_ROOT=/srv/jn-mcp/snapshots/engine \
deploy/refresh_snapshot.sh --build-only
```

Review the printed commit and snapshot path, set `JN_SNAPSHOT_HOST_PATH`, then start the
gateway:

```sh
docker compose --env-file .env up -d --build gateway
```

The non-build-only refresh command can perform atomic selection, health verification,
rollback, and bounded retention after the deployment paths are configured.

## Optional Source Profile

Copy `.env.gateway-repo.example` to ignored `.env.gateway-repo`, configure a second
OAuth application and secrets directory, then build the public repository snapshot:

```sh
GATEWAY_REPO_MIRROR_PATH=/srv/jn-mcp/mirrors/contributor-mcp.git \
GATEWAY_REPO_SNAPSHOT_ROOT=/srv/jn-mcp/snapshots/contributor-mcp \
deploy/refresh_gateway_snapshot.sh --build-only
```

Start it with `docker-compose.gateway-repo.yml` and a distinct loopback port. Do not
combine the profiles under one public origin.

## HTTPS Transport

Any reverse proxy or tunnel that preserves the public origin and OAuth callback paths
may be used. The included Cloudflare file is only a placeholder. Never commit the real
configuration or tunnel credential. The tunnel container, if used, receives only its
own config and credential—not snapshots, application secrets, audit data, or a Docker
socket.

Apply edge request limits appropriate to expected contributor usage and monitor OAuth
errors without logging bearer tokens or authorization codes. Exact production rules
are operational data and should be documented privately.

## Rotation and Offboarding

- Rotate OAuth, collaborator-check, and Actions credentials independently.
- Restart the affected service after atomically replacing a secret file.
- Verify the collaborator credential with
  `scripts/check_github_collaborator_token.py` before deployment.
- Removing a GitHub collaborator revokes new checks immediately and cached positive
  decisions within the configured maximum TTL.
- Preserve and rotate the engine profile's general `tool_calls.ndjson` audit log
  outside the container, recreating the active file as UID/GID 10001 mode 0600.

### Claim-ledger compaction and recovery

`task_claims.ndjson` is versioned state as well as audit evidence. Never truncate,
replace, or rotate it while `ENABLE_WRITE_ACTIONS=true`: doing so would discard active
ownership. Monitor its size and schedule maintenance before 48 MiB; claim/release
fails closed at the hard 64 MiB bound.

Compaction deliberately uses the maximum 24-hour claim TTL instead of rewriting
history or trying to copy live state:

1. Set `ENABLE_WRITE_ACTIONS=false` and recreate the authenticated engine service.
   This temporarily disables all three write tools. Record the disable time.
2. Wait a full 24 hours so every claim accepted before the disable time has expired.
3. Stop the engine service. Atomically rename `task_claims.ndjson` to a dated archive
   in the same durable directory; never edit or delete that evidence as part of
   compaction.
4. Create an empty `task_claims.ndjson` owned by UID/GID 10001 with mode 0600, then
   restore `ENABLE_WRITE_ACTIONS=true` and recreate only the authenticated engine
   service.
5. Verify claim, replay, guarded release, and both ledger file permissions before
   ending maintenance.

Use the same procedure when an unsupported `schema_version`, malformed/partial
record, or unsafe ledger mode makes claim tools fail closed. Keep the poisoned file as
the dated forensic archive. Do not hand-edit append-only evidence. A schema upgrade
must deploy a reader for the new version before re-enabling writes.

Changing authentication mode is an explicit maintenance action. Device mode requires
its own RSA signing key, enrollment secret, and redirect allowlist. It must never be an
automatic fallback for failed GitHub authorization.
