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
- a digest-pinned `CLOUDFLARED_IMAGE` if using the optional tunnel profile

Create the secrets directory with mode 0700 and these mode-0600 files:

- `github_oauth_client_secret`
- `github_collaborator_token`
- `github_actions_read_token`
- `oauth_jwt_signing_key`

The collaborator token checks current access to the fixed JN Engine repository. The
Actions token is separate and should have only Actions read, Contents read, and Metadata
read for that repository. Generate a new signing key for each deployment; never reuse a
sample value from documentation or tests.

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
- Preserve and rotate the engine profile's audit log outside the container.

Changing authentication mode is an explicit maintenance action. Device mode requires
its own RSA signing key, enrollment secret, and redirect allowlist. It must never be an
automatic fallback for failed GitHub authorization.
