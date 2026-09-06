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

# Docker Desktop on Windows: exit 125 Access is denied for %TEMP% bind-mounts.
# Unix /tmp is fine; do not treat $TMPDIR as hostile.
is_hostile_temp_bind_source() {
  local raw="${1:-}"
  [[ -z "$raw" ]] && return 1
  local n
  n="$(printf '%s' "$raw" | tr '\\' '/' | tr '[:upper:]' '[:lower:]')"
  case "$n" in
    *'/appdata/local/temp'|*'/appdata/local/temp/'*) return 0 ;;
  esac
  # Harness prefixes: rle-grok-home-* (GROK_HOME) and rle-grok-* (--cwd).
  case "$n" in
    *'/temp/rle-grok'*) return 0 ;;
  esac
  return 1
}

warn_skip_temp_mount() {
  local role="$1"
  local mount_path="$2"
  echo "warning: skipping Docker bind-mount of hostile temp ${role} '${mount_path}'. Docker Desktop on Windows returns exit 125 Access is denied for %TEMP% / AppData\\Local\\Temp mounts. Relying on MCP_URL env and an empty container GROK_HOME (entrypoint writes config). Session resume across --rm ticks needs a non-Temp volume (set GROK_DOCKER_HOME_VOLUME)." >&2
}

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

# Named volume persists /home/grok/.grok across --rm ticks without %TEMP%.
# Else mount a non-temp harness GROK_HOME. Never mount ~/.grok.
# Hostile Windows Temp paths are skipped: MCP_URL + empty container home.
if [[ -n "${GROK_DOCKER_HOME_VOLUME:-}" ]]; then
  docker_args+=(-v "${GROK_DOCKER_HOME_VOLUME}:/home/grok/.grok")
elif [[ -n "${GROK_HOME:-}" && -d "${GROK_HOME}" ]]; then
  host_default="${HOME}/.grok"
  if [[ "$(cd "$GROK_HOME" && pwd)" == "$(cd "$host_default" 2>/dev/null && pwd)" ]]; then
    echo "refusing to mount host ~/.grok (plugin zoo). Using empty container home." >&2
  elif is_hostile_temp_bind_source "$GROK_HOME"; then
    warn_skip_temp_mount "GROK_HOME" "$GROK_HOME"
  else
    docker_args+=(-v "${GROK_HOME}:/home/grok/.grok")
  fi
fi

if [[ -n "$cwd_host" && -d "$cwd_host" ]]; then
  if is_hostile_temp_bind_source "$cwd_host"; then
    warn_skip_temp_mount "--cwd" "$cwd_host"
  else
    docker_args+=(-v "${cwd_host}:/work")
  fi
fi

exec docker run "${docker_args[@]}" "$IMAGE" "${out[@]}"
