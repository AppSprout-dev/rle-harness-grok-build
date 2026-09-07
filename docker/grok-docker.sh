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

PERSIST_ACTION="${RLE_GROK_PERSIST_ACTION:-}"
PERSIST_CONTAINER="${RLE_GROK_PERSIST_CONTAINER:-}"

if [[ -n "$PERSIST_ACTION" && "$PERSIST_ACTION" != "start" && "$PERSIST_ACTION" != "exec" && "$PERSIST_ACTION" != "stop" ]]; then
  echo "unknown RLE_GROK_PERSIST_ACTION=${PERSIST_ACTION} (expected start|exec|stop)" >&2
  exit 1
fi

# Stop does not need grok argv / python / ARGV_JSON.
if [[ "$PERSIST_ACTION" == "stop" ]]; then
  if [[ -z "$PERSIST_CONTAINER" ]]; then
    echo "RLE_GROK_PERSIST_ACTION=stop requires RLE_GROK_PERSIST_CONTAINER" >&2
    exit 1
  fi
  docker stop --time 10 "$PERSIST_CONTAINER" || true
  docker rm -f "$PERSIST_CONTAINER" || true
  exit 0
fi

# Windows cmd.exe %* drops quoted/large -p prompts. The harness writes argv
# (after the wrapper path) as UTF-8 JSON and sets RLE_GROK_ARGV_JSON.
grok_args=()
if [[ -n "${RLE_GROK_ARGV_JSON:-}" ]]; then
  if [[ ! -f "${RLE_GROK_ARGV_JSON}" ]]; then
    echo "RLE_GROK_ARGV_JSON is set but file not found: ${RLE_GROK_ARGV_JSON}" >&2
    exit 1
  fi
  py=""
  if command -v python3 >/dev/null 2>&1; then
    py=python3
  elif command -v python >/dev/null 2>&1; then
    py=python
  else
    echo "RLE_GROK_ARGV_JSON is set but python3 (or python) is required to load it." >&2
    exit 1
  fi
  "$py" -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
    sys.stderr.write("RLE_GROK_ARGV_JSON must be a JSON array of strings\n")
    sys.exit(1)
' "${RLE_GROK_ARGV_JSON}"
  mapfile -d '' grok_args < <("$py" -c '
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
sys.stdout.buffer.write(b"\0".join(s.encode("utf-8") for s in data))
if data:
    sys.stdout.buffer.write(b"\0")
' "${RLE_GROK_ARGV_JSON}")
  if ((${#grok_args[@]})) && [[ -z "${grok_args[-1]}" ]]; then
    unset 'grok_args[-1]'
  fi
else
  grok_args=("$@")
fi

cwd_host=""
out=()
prev=""
for a in "${grok_args[@]}"; do
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

# Forward API key env vars by name. Never bake secrets into the image/config.
append_compat_env() {
  local -n _dest=$1
  if [[ -n "${XAI_API_KEY:-}" ]]; then
    _dest+=(-e "XAI_API_KEY=${XAI_API_KEY}")
  fi
  if [[ -n "${OPENROUTER_API_KEY:-}" ]]; then
    _dest+=(-e "OPENROUTER_API_KEY=${OPENROUTER_API_KEY}")
  fi
  for var in OPENAI_COMPAT GROK_PROVIDER GROK_MODEL GROK_BASE_URL GROK_API_KEY_ENV GROK_API_BACKEND; do
    if [[ -n "${!var:-}" ]]; then
      _dest+=(-e "${var}=${!var}")
    fi
  done
  local key_env="${GROK_API_KEY_ENV:-}"
  if [[ -n "$key_env" && "$key_env" != "XAI_API_KEY" && "$key_env" != "OPENROUTER_API_KEY" ]]; then
    if [[ -n "${!key_env:-}" ]]; then
      _dest+=(-e "${key_env}=${!key_env}")
    fi
  fi
}

docker_args=(
  --add-host=host.docker.internal:host-gateway
  -e "MCP_URL=${MCP_URL}"
  -e "GROK_HOME=/home/grok/.grok"
)
append_compat_env docker_args
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

if [[ "$PERSIST_ACTION" == "exec" ]]; then
  if [[ -z "$PERSIST_CONTAINER" ]]; then
    echo "RLE_GROK_PERSIST_ACTION=exec requires RLE_GROK_PERSIST_CONTAINER" >&2
    exit 1
  fi
  exec_args=(
    -e "MCP_URL=${MCP_URL}"
    -e "GROK_HOME=/home/grok/.grok"
  )
  append_compat_env exec_args
  exec docker exec "${exec_args[@]}" "$PERSIST_CONTAINER" /entrypoint.sh "${out[@]}"
fi

if [[ "$PERSIST_ACTION" == "start" ]]; then
  if [[ -z "$PERSIST_CONTAINER" ]]; then
    echo "RLE_GROK_PERSIST_ACTION=start requires RLE_GROK_PERSIST_CONTAINER" >&2
    exit 1
  fi
  docker rm -f "$PERSIST_CONTAINER" >/dev/null 2>&1 || true
  image_cmd=(persist)
  # ACP: publish host:port:2419 and run documented grok agent serve as PID 1.
  if [[ -n "${RLE_GROK_ACP_PUBLISH:-}" ]]; then
    docker_args+=(-p "${RLE_GROK_ACP_PUBLISH}")
    if [[ -n "${GROK_AGENT_SECRET:-}" ]]; then
      docker_args+=(-e "GROK_AGENT_SECRET=${GROK_AGENT_SECRET}")
    fi
    # --cwd was rewritten for the /work bind-mount; do not forward it (or
    # other grok -p flags) into acp-serve → grok agent (1.0.13 exit 2).
    agent_flags=()
    skip_next=0
    for a in "${out[@]}"; do
      if ((skip_next)); then
        skip_next=0
        continue
      fi
      if [[ "$a" == "persist" || "$a" == "acp-serve" ]]; then
        continue
      fi
      case "$a" in
        --cwd|--max-turns|--disallowed-tools|--output-format|--resume|-r|--reasoning-effort|--effort|--session-id|-s|--prompt-json|--prompt-file|--permission-mode|--tools)
          skip_next=1
          continue
          ;;
        --cwd=*|--max-turns=*|--disallowed-tools=*|--output-format=*|--resume=*|-r=*|--reasoning-effort=*|--effort=*|--session-id=*|-s=*|--prompt-json=*|--prompt-file=*|--permission-mode=*|--tools=*)
          continue
          ;;
        --no-subagents|--no-plan|--yolo|-p|--single|--include-partial-messages|--fork-session|--continue|-c|--no-memory|--disable-web-search)
          continue
          ;;
      esac
      agent_flags+=("$a")
    done
    image_cmd=(acp-serve "${agent_flags[@]}")
  fi
  # Detached, named, no --rm.
  exec docker run -d --name "$PERSIST_CONTAINER" "${docker_args[@]}" "$IMAGE" "${image_cmd[@]}"
fi

exec docker run --rm "${docker_args[@]}" "$IMAGE" "${out[@]}"
