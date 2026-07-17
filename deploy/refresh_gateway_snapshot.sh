#!/bin/sh
set -eu

REPOSITORY_URL='https://github.com/alexscott2718-gif/jn-engine-contributor-mcp'
REF='refs/heads/main'
REMOTE_TRACKING_REF='refs/remotes/origin/main'
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPOSITORY_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PYTHON="$REPOSITORY_DIR/.venv/bin/python"
SNAPSHOT_ENV_FILE="$SCRIPT_DIR/gateway-snapshot.env"
COMPOSE_FILE="$REPOSITORY_DIR/docker-compose.gateway-repo.yml"
GATEWAY_ENV_FILE="$REPOSITORY_DIR/.env.gateway-repo"

build_only=false
case "${1-}" in
  "") ;;
  "--build-only") build_only=true ;;
  *)
    printf '%s\n' 'usage: refresh_gateway_snapshot.sh [--build-only]' >&2
    exit 64
    ;;
esac

: "${GATEWAY_REPO_MIRROR_PATH:?GATEWAY_REPO_MIRROR_PATH is required}"
: "${GATEWAY_REPO_SNAPSHOT_ROOT:?GATEWAY_REPO_SNAPSHOT_ROOT is required}"

if [ ! -x "$PYTHON" ]; then
  printf '%s\n' 'project virtualenv is missing; install locked dependencies first' >&2
  exit 69
fi

mkdir -p -- "$GATEWAY_REPO_SNAPSHOT_ROOT"
lock_file="$GATEWAY_REPO_SNAPSHOT_ROOT/.refresh.lock"
exec 9>"$lock_file"
if ! flock -n 9; then
  printf '%s\n' 'another gateway snapshot refresh is already running' >&2
  exit 75
fi

if [ ! -d "$GATEWAY_REPO_MIRROR_PATH/objects" ]; then
  mkdir -p -- "$(dirname -- "$GATEWAY_REPO_MIRROR_PATH")"
  git init --bare -- "$GATEWAY_REPO_MIRROR_PATH" >/dev/null
fi

if git --git-dir="$GATEWAY_REPO_MIRROR_PATH" remote get-url origin >/dev/null 2>&1; then
  configured_url=$(git --git-dir="$GATEWAY_REPO_MIRROR_PATH" remote get-url origin)
  if [ "$configured_url" != "$REPOSITORY_URL" ]; then
    printf '%s\n' 'gateway snapshot mirror origin does not match frozen URL' >&2
    exit 78
  fi
else
  git --git-dir="$GATEWAY_REPO_MIRROR_PATH" remote add origin "$REPOSITORY_URL"
fi

git --git-dir="$GATEWAY_REPO_MIRROR_PATH" fetch \
  --force --no-tags --prune origin "$REF:$REMOTE_TRACKING_REF"
commit=$(git --git-dir="$GATEWAY_REPO_MIRROR_PATH" rev-parse --verify "$REMOTE_TRACKING_REF^{commit}")
tree=$(git --git-dir="$GATEWAY_REPO_MIRROR_PATH" rev-parse --verify "$commit^{tree}")
commit_time=$(git --git-dir="$GATEWAY_REPO_MIRROR_PATH" show -s --format=%cI "$commit")
target="$GATEWAY_REPO_SNAPSHOT_ROOT/$commit"

stage=
cleanup() {
  if [ -n "$stage" ] && [ -d "$stage" ]; then
    chmod -R u+w -- "$stage" 2>/dev/null || true
    rm -rf -- "$stage"
  fi
}
trap cleanup EXIT HUP INT TERM

if [ -d "$target" ]; then
  (cd "$REPOSITORY_DIR" && "$PYTHON" -m scripts.validate_gateway_snapshot "$target")
else
  stage=$(mktemp -d "$GATEWAY_REPO_SNAPSHOT_ROOT/.staging.XXXXXXXX")
  mkdir -- "$stage/content"
  git --git-dir="$GATEWAY_REPO_MIRROR_PATH" archive --format=tar "$commit" \
    | tar -xf - -C "$stage/content"
  (
    cd "$REPOSITORY_DIR"
    "$PYTHON" -m deploy.export_gateway_snapshot \
      --snapshot "$stage" \
      --commit "$commit" \
      --tree "$tree" \
      --commit-time "$commit_time"
    "$PYTHON" -m scripts.validate_gateway_snapshot --allow-writable "$stage"
  )
  find "$stage" -type f -exec chmod 0444 {} +
  find "$stage" -type d -exec chmod 0555 {} +
  mv -- "$stage" "$target"
  stage=
  (cd "$REPOSITORY_DIR" && "$PYTHON" -m scripts.validate_gateway_snapshot "$target")
fi

printf 'snapshot repository=%s ref=%s commit=%s tree=%s\n' \
  'alexscott2718-gif/jn-engine-contributor-mcp' "$REF" "$commit" "$tree"

if [ "$build_only" = true ]; then
  printf 'gateway snapshot build-only promotion complete: %s\n' "$target"
  exit 0
fi

if [ ! -f "$GATEWAY_ENV_FILE" ]; then
  printf '%s\n' 'dedicated gateway repository env is missing' >&2
  exit 78
fi

previous_target=
if [ -f "$SNAPSHOT_ENV_FILE" ]; then
  previous_target=$(sed -n 's/^GATEWAY_REPO_SNAPSHOT_HOST_PATH=//p' "$SNAPSHOT_ENV_FILE")
fi

env_tmp="$SNAPSHOT_ENV_FILE.tmp.$$"
umask 077
printf 'GATEWAY_REPO_SNAPSHOT_HOST_PATH=%s\n' "$target" >"$env_tmp"
mv -- "$env_tmp" "$SNAPSHOT_ENV_FILE"

compose() {
  docker compose \
    --project-directory "$REPOSITORY_DIR" \
    --env-file "$GATEWAY_ENV_FILE" \
    --env-file "$SNAPSHOT_ENV_FILE" \
    -f "$COMPOSE_FILE" \
    --profile gateway-repo \
    "$@"
}

compose up -d --build --force-recreate gateway-repo
api_port=$(sed -n 's/^GATEWAY_REPO_PUBLISHED_API_PORT=//p' "$GATEWAY_ENV_FILE" | tail -n 1)
api_port=${api_port:-8789}

healthy=false
attempt=1
while [ "$attempt" -le 30 ]; do
  if "$PYTHON" -c \
    'import json,sys,urllib.request; data=json.load(urllib.request.urlopen(sys.argv[1], timeout=2)); raise SystemExit(0 if data.get("commit") == sys.argv[2] else 1)' \
    "http://127.0.0.1:$api_port/health" "$commit"
  then
    healthy=true
    break
  fi
  sleep 2
  attempt=$((attempt + 1))
done

if [ "$healthy" != true ]; then
  printf '%s\n' 'new gateway snapshot failed verification; rolling back' >&2
  if [ -n "$previous_target" ] && [ -d "$previous_target" ]; then
    printf 'GATEWAY_REPO_SNAPSHOT_HOST_PATH=%s\n' "$previous_target" >"$env_tmp"
    mv -- "$env_tmp" "$SNAPSHOT_ENV_FILE"
    compose up -d --build --force-recreate gateway-repo
  fi
  exit 1
fi

(
  cd "$REPOSITORY_DIR"
  "$PYTHON" -m deploy.prune_snapshots \
    --snapshot-root "$GATEWAY_REPO_SNAPSHOT_ROOT" \
    --current "$target" \
    --keep 3
)
printf 'gateway repository MCP healthy: commit=%s path=/mcp\n' "$commit"
