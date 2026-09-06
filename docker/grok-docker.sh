#!/usr/bin/env bash
# Drop-in `grok` that runs the stock Linux binary in Docker.
# Usage: ./docker/grok-docker.sh mcp list
#        --harness-opt binary=/path/to/docker/grok-docker.sh
set -euo pipefail

IMAGE="${GROK_DOCKER_IMAGE:-rle-grok-build:local}"
MCP_URL="${MCP_URL:-http://host.docker.internal:8766/mcp}"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker CLI not found. Install Docker Desktop and retry." >&2
  exit 127
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker CLI is present but the daemon is not running. Start Docker Desktop." >&2
  exit 1
fi

cwd_host=""
out=()
prev=""
for a in "$@"; do
  if [[ "$prev" == "--cwd" ]]; then
    cwd_host="$a"
    out+=("/work")
    prev=""
    continue
  fi
  if [[ "$a" == --cwd=* ]]; then
    cwd_host="${a#--cwd=}"
    out+=("--cwd=/work")
    continue
  fi
  out+=("$a")
  prev="$a"
done

docker_args=(
  --rm
  --add-host=host.docker.internal:host-gateway
  -e "MCP_URL=${MCP_URL}"
  -e "GROK_HOME=/home/grok/.grok"
)
if [[ -n "${XAI_API_KEY:-}" ]]; then
  docker_args+=(-e "XAI_API_KEY=${XAI_API_KEY}")
fi
if [[ -n "${GROK_AUTH_JSON:-}" && -s "${GROK_AUTH_JSON}" ]]; then
  docker_args+=(-v "${GROK_AUTH_JSON}:/auth/auth.json:ro")
fi

# Mount the harness temp GROK_HOME (session/auth). Never mount ~/.grok.
if [[ -n "${GROK_HOME:-}" && -d "${GROK_HOME}" ]]; then
  host_default="${HOME}/.grok"
  if [[ "$(cd "$GROK_HOME" && pwd)" == "$(cd "$host_default" 2>/dev/null && pwd)" ]]; then
    echo "refusing to mount host ~/.grok (plugin zoo). Using empty container home." >&2
  else
    docker_args+=(-v "${GROK_HOME}:/home/grok/.grok")
  fi
fi

if [[ -n "$cwd_host" && -d "$cwd_host" ]]; then
  docker_args+=(-v "${cwd_host}:/work")
fi

exec docker run "${docker_args[@]}" "$IMAGE" "${out[@]}"
