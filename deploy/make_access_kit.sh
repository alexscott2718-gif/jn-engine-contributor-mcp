#!/bin/sh
# Build a credential-free contributor onboarding archive from reviewed docs.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPOSITORY_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
OUTPUT=${1:-"$REPOSITORY_DIR/dist/jn-engine-contributor-mcp-access-kit.tar.gz"}
STAGE=

cleanup() {
  if [ -n "$STAGE" ] && [ -d "$STAGE" ]; then
    rm -rf -- "$STAGE"
  fi
}
trap cleanup EXIT HUP INT TERM

mkdir -p -- "$(dirname -- "$OUTPUT")"
STAGE=$(mktemp -d)
KIT="$STAGE/jn-engine-contributor-mcp-access-kit"
mkdir -- "$KIT"

cat >"$KIT/README.md" <<'EOF'
# JN Engine AI Gateway Access Kit

Start with `onboarding_contributor.md`. The MCP endpoint is
`https://mcp.example.org/mcp`; authenticate with your own GitHub identity and leave
client ID/secret fields blank when the client supports Dynamic Client Registration.

This archive is intentionally credential-free. It contains no OAuth client secret,
collaborator credential, bearer token, signing key, enrollment secret, or private
vault content.
EOF

cat >"$KIT/endpoint.json" <<'EOF'
{
  "app": "jn-engine-contributor-mcp",
  "mode": "read_only",
  "public_base_url": "https://mcp.example.org",
  "mcp_url": "https://mcp.example.org/mcp",
  "health_url": "https://mcp.example.org/health",
  "repository": "alexscott2718-gif/jn-engine",
  "ref": "refs/heads/master"
}
EOF

for source in \
  docs/onboarding_contributor.md \
  docs/mcp_surface.md \
  docs/security_model.md \
  docs/api_usage.md
do
  if [ ! -f "$REPOSITORY_DIR/$source" ]; then
    printf 'required access-kit file is missing: %s\n' "$source" >&2
    exit 1
  fi
  cp -- "$REPOSITORY_DIR/$source" "$KIT/$(basename -- "$source")"
done

(
  cd "$KIT"
  sha256sum README.md endpoint.json onboarding_contributor.md mcp_surface.md \
    security_model.md api_usage.md >SHA256SUMS
)

tar -C "$STAGE" -czf "$OUTPUT" jn-engine-contributor-mcp-access-kit

if tar -xzf "$OUTPUT" -O 2>/dev/null | grep -E -n \
  'BEGIN [A-Z ]*PRIVATE KEY|eyJ[A-Za-z0-9_-]{10,}\.eyJ|github_pat_[A-Za-z0-9_]{20,}|(API_TOKEN|MCP_ENROLLMENT_SECRET|GITHUB_OAUTH_CLIENT_SECRET|GITHUB_COLLABORATOR_TOKEN)=[^[:space:]<]{8,}'
then
  printf '%s\n' 'possible secret found in access kit; archive rejected' >&2
  rm -f -- "$OUTPUT"
  exit 1
fi

printf 'access kit: %s\n' "$OUTPUT"
tar -tzf "$OUTPUT" | sed 's/^/  /'
printf '%s\n' 'secret scan: clean'
