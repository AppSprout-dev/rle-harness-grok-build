#!/usr/bin/env bash
# Stock grok: empty GROK_HOME, RLE-only config, no host plugin zoo.
set -euo pipefail

export PATH="/usr/local/bin:${PATH}"
export HOME="${HOME:-/home/grok}"
export GROK_HOME="${GROK_HOME:-${HOME}/.grok}"
MCP_URL="${MCP_URL:-http://host.docker.internal:8766/mcp}"

unset CLAUDE_CONFIG_DIR || true
unset CURSOR_CONFIG_DIR || true

mkdir -p "$GROK_HOME"
rm -rf "$GROK_HOME/plugins" "$GROK_HOME/skills"
rm -f "$GROK_HOME/mcp.json" "$GROK_HOME/managed_config.toml" "$GROK_HOME/requirements.toml"

toml_basic_string() {
  # Quote for TOML keys/values. Model ids may contain '/' (OpenRouter).
  local s=$1
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  printf '"%s"' "$s"
}

# OpenAI-compat / OpenRouter: same [model.<id>] shape as isolated_home.mcp_config_toml.
# Secrets stay in the env named by env_key — never written into config.toml.
compat_model=""
compat_base_url="${GROK_BASE_URL:-}"
compat_env_key="${GROK_API_KEY_ENV:-}"
compat_backend="${GROK_API_BACKEND:-chat_completions}"
compat_provider="$(printf '%s' "${GROK_PROVIDER:-}" | tr '[:upper:]' '[:lower:]')"
compat_openai="$(printf '%s' "${OPENAI_COMPAT:-}" | tr '[:upper:]' '[:lower:]')"
if [[ "$compat_openai" == "true" || "$compat_openai" == "1" || "$compat_openai" == "yes" || "$compat_provider" == "openrouter" ]]; then
  if [[ -z "$compat_base_url" ]]; then
    compat_base_url="https://openrouter.ai/api/v1"
  fi
  if [[ -z "$compat_env_key" ]]; then
    compat_env_key="OPENROUTER_API_KEY"
  fi
fi
if [[ -n "${GROK_MODEL:-}" && -n "$compat_base_url" ]]; then
  compat_model="$GROK_MODEL"
  if [[ -z "$compat_env_key" ]]; then
    compat_env_key="OPENROUTER_API_KEY"
  fi
fi

write_config() {
  local dest="$1"
  mkdir -p "$(dirname "$dest")"
  cat > "$dest" <<EOF
[mcp_servers.rle]
url = "${MCP_URL}"
startup_timeout_sec = 30
headers = { "x-mcp-session-id" = "{{session_id}}" }

[compat.claude]
mcps = false

[compat.cursor]
mcps = false
EOF
  if [[ -n "$compat_model" && -n "$compat_base_url" ]]; then
    {
      printf '\n[model.%s]\n' "$(toml_basic_string "$compat_model")"
      printf 'model = %s\n' "$(toml_basic_string "$compat_model")"
      printf 'base_url = %s\n' "$(toml_basic_string "$compat_base_url")"
      printf 'env_key = %s\n' "$(toml_basic_string "$compat_env_key")"
      printf 'api_backend = %s\n' "$(toml_basic_string "$compat_backend")"
    } >> "$dest"
  fi
}

write_config "$GROK_HOME/config.toml"
# Harness also writes project-scoped .grok/config.toml; rewrite so cwd
# priority cannot resurrect 127.0.0.1 bind URLs.
if [[ -d /work/.grok ]] || [[ -d .grok ]]; then
  write_config "/work/.grok/config.toml"
fi

if [[ -s /auth/auth.json ]]; then
  cp /auth/auth.json "$GROK_HOME/auth.json"
fi

if [[ $# -eq 0 ]]; then
  set -- mcp list
fi

# Warm persist: write isolated config once, then keep the container alive so
# the host can `docker exec /entrypoint.sh grok -p … --resume` each tick.
# `docker exec` skips the image ENTRYPOINT, so wrappers call this path.
if [[ "${1:-}" == "persist" ]]; then
  exec sleep infinity
fi

# ACP serve: same isolated home, then documented grok agent serve.
# Host publishes the port (RLE_GROK_ACP_PUBLISH) and talks JSON-RPC over /ws.
# Never forward grok -p flags (--cwd, --max-turns, --disallowed-tools, …);
# pinned grok 1.0.13 rejects them and the persist container exits.
if [[ "${1:-}" == "acp-serve" ]]; then
  shift
  bind="${GROK_ACP_BIND:-0.0.0.0:2419}"
  if [[ -z "${GROK_AGENT_SECRET:-}" ]]; then
    echo "acp-serve requires GROK_AGENT_SECRET" >&2
    exit 1
  fi
  agent_opts=()
  skip_next=0
  for a in "$@"; do
    if ((skip_next)); then
      skip_next=0
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
    agent_opts+=("$a")
  done
  exec grok agent --always-approve "${agent_opts[@]}" \
    serve --bind "$bind" --secret "$GROK_AGENT_SECRET"
fi

exec grok "$@"
