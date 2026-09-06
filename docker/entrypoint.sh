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
if [[ "${1:-}" == "acp-serve" ]]; then
  shift
  bind="${GROK_ACP_BIND:-0.0.0.0:2419}"
  if [[ -z "${GROK_AGENT_SECRET:-}" ]]; then
    echo "acp-serve requires GROK_AGENT_SECRET" >&2
    exit 1
  fi
  exec grok agent --always-approve --no-subagents --no-plan "$@" \
    serve --bind "$bind" --secret "$GROK_AGENT_SECRET"
fi

exec grok "$@"
