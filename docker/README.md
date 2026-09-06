# Stock grok-build Docker (plugin-free)

One image: official Linux `grok` + this Python harness + RLE. Runtime
`GROK_HOME` is empty except an RLE-only `config.toml` (compat Claude/Cursor
MCPs off). Auth is `XAI_API_KEY` or a mounted `auth.json` file.

This is the escape hatch when host Windows grok still loads desktop plugins
after the GROK_HOME isolation patches (PRs #1 / #2).

RimAPI stays outside this image. Talk to RLE's headless RimWorld on port
**8765** (`rle.docker.DEFAULT_PORT`, compose service `rimworld`).

## Build

```bash
docker build -f docker/Dockerfile -t rle-grok-build:local .
```

Pin a grok release if you want a reproducible binary:

```bash
docker build -f docker/Dockerfile --build-arg GROK_VERSION=1.0.13 -t rle-grok-build:local .
```

The Dockerfile downloads the stock Linux artifact from
`https://x.ai/cli/install.sh` (linux-x86_64 / linux-aarch64). No host grok
binary is copied in.

## Auth (do not bake secrets)

Preferred:

```bash
export XAI_API_KEY=xai-...
```

Or mount **only** `auth.json` (the file produced by `grok login`, not the
whole `~/.grok` tree):

```bash
docker run --rm \
  -e XAI_API_KEY \
  -v /abs/path/to/auth.json:/auth/auth.json:ro \
  rle-grok-build:local smoke
```

## Smoke (no RimWorld)

Proves `grok mcp list` sees **rle** and not wandb/claude/cursor:

```bash
docker run --rm rle-grok-build:local smoke
```

Optional: in-process MockRimAPI + headless `-p` tool call (~60s, needs a key):

```bash
docker run --rm -e XAI_API_KEY rle-grok-build:local \
  smoke --call-tool --model grok-4.6
```

## One-tick Crashlanded (RimAPI required)

Docker Desktop / host RimAPI on 8765 (`extra_hosts` is in compose):

```bash
docker compose -f docker/docker-compose.yml run --rm grok-harness \
  cal --ticks 1 --scenario crashlanded --model grok-4.6
```

Same with a raw `docker run`:

```bash
docker run --rm --add-host=host.docker.internal:host-gateway \
  -e XAI_API_KEY \
  -e RIMAPI_URL=http://host.docker.internal:8765 \
  rle-grok-build:local \
  cal --ticks 1 --scenario crashlanded --model grok-4.6
```

### Join RLE's existing compose network

RLE's `docker/docker-compose.yml` publishes `rimworld:8765`. After that stack
is up:

```bash
docker network ls   # note the project_default network
export RLE_COMPOSE_NETWORK=docker_default
docker compose -f docker/docker-compose.yml \
  -f docker/docker-compose.rle-network.yml \
  run --rm grok-harness cal --ticks 1
```

That overlay sets `RIMAPI_URL=http://rimworld:8765`.

## What is deliberately NOT mounted

| Host path | Why |
|---|---|
| `~/.grok` | Plugins, skills, installer `config.toml`, MCP zoo |
| `~/.grok/config.toml` | Compat MCP imports (Claude/Cursor) |
| `~/.claude.json` / `~/.claude` | Claude compatibility MCP discovery |
| `~/.cursor` | Cursor compatibility MCP discovery |

The entrypoint also unsets `CLAUDE_CONFIG_DIR` / `CURSOR_CONFIG_DIR`.

## Design note

**grok + harness in one image** (not a grok-only sidecar). RLE hosts the MCP
server in-process on localhost inside the container; grok talks to that. The
container only needs network reachability to RimAPI (and xAI for the model).
Host-side `python scripts/run_scenario.py --harness grok-build` is unchanged.
