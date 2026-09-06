# Stock Linux grok (plugin-free sidecar)

Container = **official Linux `grok` only**. Windows Steam RimWorld + RIMAPI
stay on the **host** (`localhost:8765`). This image does not run RimWorld.

Host desktop grok loads Claude/Cursor compat MCPs and plugins even after
GROK_HOME isolation (PRs #1 / #2). This sidecar starts from an empty
`GROK_HOME` and writes an RLE-only `config.toml`.

RLE's own Linux headless RimWorld image (`AppSprout-dev/RLE/docker/`) is a
**separate** path. Third-party harness Docker stays in this repo.

## Prerequisite — RLE `McpHost` bind (sibling PR)

Today RLE's `McpHost` binds `127.0.0.1` plus an ephemeral port. A process
inside Docker **cannot** reach that.

The sibling RLE change must:

1. Bind `0.0.0.0` (not loopback-only)
2. Use a fixed / configurable port (default **8766**)
3. Advertise `http://host.docker.internal:8766/mcp` to agents in Docker

This repo's compose and wrappers assume that advertised URL and pass
`--add-host host.docker.internal:host-gateway`.

Until that lands:

* `grok mcp list` smoke (CI / local) works — it only reads isolated config
* Live `rle__*` tool calls from the container will fail to connect

Host RimAPI remains `http://127.0.0.1:8765`. The MCP server (in the host
Python harness) is what grok-in-Docker must dial, on **8766**.

## Build (pinned Linux grok)

Default pin is `GROK_VERSION=1.0.13` (linux-x86_64 / linux-aarch64).

```bash
docker build -f docker/Dockerfile -t rle-grok-build:local .
```

Docker Desktop CLI may be installed while the **daemon is stopped**. Start
Docker Desktop before build/run; wrappers exit with a clear error otherwise.

## Auth (do not bake secrets)

Preferred:

```bash
export XAI_API_KEY=xai-...
```

Or mount **only** `auth.json` (not the whole `~/.grok` tree):

```bash
docker run --rm --add-host=host.docker.internal:host-gateway \
  -e XAI_API_KEY -e MCP_URL=http://host.docker.internal:8766/mcp \
  -v /abs/path/to/auth.json:/auth/auth.json:ro \
  rle-grok-build:local mcp list
```

## Smoke (no RimWorld, CI-safe)

```bash
docker run --rm rle-grok-build:local mcp list
# or
./docker/grok-docker.sh mcp list          # Unix
.\docker\grok-docker.cmd mcp list        # Windows
```

Expect **rle** and not wandb/claude/cursor as MCP servers.

## Run wrappers (host harness → container grok)

After the RLE McpHost PR, point the host harness at the wrapper so each
`grok -p` tick is stock Linux grok:

```powershell
$env:XAI_API_KEY = "xai-..."
$env:MCP_URL = "http://host.docker.internal:8766/mcp"
python scripts/run_scenario.py crashlanded --harness grok-build `
  --model grok-4.6 --ticks 1 `
  --harness-opt "binary=C:\path\to\rle-harness-grok-build\docker\grok-docker.cmd" `
  --harness-opt "mcp_advertise_url=http://host.docker.internal:8766/mcp"
```

Unix:

```bash
python scripts/run_scenario.py crashlanded --harness grok-build \
  --harness-opt binary=./docker/grok-docker.sh \
  --harness-opt mcp_advertise_url=http://host.docker.internal:8766/mcp
```

Wrappers:

* inject `host.docker.internal:host-gateway`
* rewrite `--cwd <host>` → mount + `/work`
* mount a **temp** `GROK_HOME` (harness-created) but **refuse** `~/.grok`
* forward `XAI_API_KEY` / `GROK_AUTH_JSON`

## What is deliberately NOT mounted

| Host path | Why |
|---|---|
| `~/.grok` | Plugins, skills, installer `config.toml`, MCP zoo |
| `~/.grok/config.toml` | Compat MCP imports |
| `~/.claude.json` / `~/.claude` | Claude compatibility MCP discovery |
| `~/.cursor` | Cursor compatibility MCP discovery |

The entrypoint unsets `CLAUDE_CONFIG_DIR` / `CURSOR_CONFIG_DIR` and rewrites
`config.toml` from `MCP_URL` on every start.
