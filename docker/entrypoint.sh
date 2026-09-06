#!/usr/bin/env bash
# Isolated stock grok: empty GROK_HOME, no host plugin zoo, RLE MCP only.
set -euo pipefail

export PATH="/usr/local/bin:${PATH}"
export HOME="${HOME:-/home/rle}"
export GROK_HOME="${GROK_HOME:-${HOME}/.grok}"

# Host Claude/Cursor MCP discovery must not leak into this process.
unset CLAUDE_CONFIG_DIR || true
unset CURSOR_CONFIG_DIR || true

if [[ $# -eq 0 ]]; then
  set -- smoke
fi

exec python -m rle_harness_grok_build.docker_cli "$@"
