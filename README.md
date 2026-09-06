# rle-harness-grok-build

[Grok Build](https://github.com/xai-org/grok-build) as an [RLE](https://github.com/AppSprout-dev/RLE) harness.

RLE benchmarks **harnesses × models** on a live RimWorld colony. This package lets xAI's
Grok Build coding agent be the harness: each tick is one headless invocation
(`grok -p … --output-format json`) that resumes the previous session, acts on the colony
through the RLE MCP tools (`rle__get_brief`, `rle__work_priority`, … `rle__end_turn`), and
the writes that reached the game are scored with the same composite as every other harness.

**OpenCode-parity lifecycle:** default stays cold-start (`docker run --rm` or
host `grok -p` every tick). `--harness-opt warm=true` keeps one Docker sidecar
and `docker exec`s `grok -p` each tick. `--harness-opt acp=true` (alias
`mode=acp`) starts long-lived `grok agent serve` and drives each tick over ACP
— see [ACP agent serve](#acp-agent-serve-opencode-parity).

## Install

```bash
# Grok Build itself (or build from source: cargo build -p xai-grok-pager-bin --release)
curl -fsSL https://x.ai/cli/install.sh | bash
export XAI_API_KEY=xai-...        # or `grok login --device-auth`

# RLE core (not on PyPI yet) + this harness
uv pip install "rimworld-learning-environment[mcp] @ git+https://github.com/AppSprout-dev/RLE"
uv pip install git+https://github.com/AppSprout-dev/rle-harness-grok-build
```

## Run

From an RLE checkout with RimWorld + RIMAPI running:

```bash
python scripts/run_benchmark.py --harness list
python scripts/run_scenario.py crashlanded --harness grok-build --model grok-4.6 --ticks 10 --tick-interval 30
python scripts/run_benchmark.py --harness grok-build --harness felix --runs 4
```

Local-first models work too: point `~/.grok/config.toml` at an OpenAI-compatible
`base_url` (see Grok Build's `11-custom-models.md`) and pass that model name with `--model`.

Options (`--harness-opt key=value`):

| Option | Default | Meaning |
|---|---|---|
| `binary` | `grok` | Executable name/path |
| `warm` / `persistent` | false | Start one grok-docker container in setup; `docker exec` each tick |
| `acp` / `mode=acp` | false | Long-lived `grok agent serve`; each tick is ACP `session/prompt` |
| `acp_bind` | `127.0.0.1:<ephemeral>` | Host `host:port` for serve (Docker publishes this to container `:2419`) |
| `acp_secret` | generated | Serve auth (`Authorization: Bearer` / `?server-key=`) |
| `resume_session` | true | `--resume <sessionId>` every tick so context carries over |
| `max_turns` | 20 | `--max-turns` cap on agentic rounds per tick |
| `reasoning_effort` | – | `--reasoning-effort` |
| `disallowed_tools` | shell/edit/web tools | Built-ins removed so the agent can only act via RLE tools |
| `turn_timeout_s` | 180 | Kill the invocation after this many seconds |
| `extra_instructions` | – | Appended to every turn prompt |
| `extra_args` | – | Raw flags appended to every invocation |
| `mcp_advertise_url` | – | URL written into grok config (Docker: `http://host.docker.internal:8766/mcp`) |
| `mcp_container_reachable` | RLE config | Bind MCP on `0.0.0.0:8766` and advertise `host.docker.internal` |

## Docker (stock Linux grok only)

**Locked architecture:** Windows RimWorld + RIMAPI stay on the **host**
(`localhost:8765`). Do not run Windows Steam RimWorld inside the Linux
container. The image is **official Linux `grok` only** — not this Python
package, not RLE, not RimWorld. RLE's Linux headless RimWorld Docker
(`AppSprout-dev/RLE/docker/`) is a separate path; third-party harness Docker
stays here.

Host desktop grok still loads plugins / Claude-Cursor compat MCPs after the
GROK_HOME isolation patches (PRs #1 / #2). The sidecar starts from an empty
`GROK_HOME` and writes RLE-only `config.toml` (`compat.*.mcps = false`).
Auth is `XAI_API_KEY` (preferred) or a mounted `auth.json` **file**. Never
mount host `~/.grok`.

### Prerequisite (RLE `McpHost` — sibling PR)

Today `McpHost` binds `127.0.0.1` + an ephemeral port → **unreachable from
Docker**. A sibling RLE PR must bind `0.0.0.0`, use a fixed port (e.g.
**8766**), and advertise `http://host.docker.internal:8766/mcp`. This repo's
compose and `docker/grok-docker.sh|.cmd` assume that URL and pass
`--add-host host.docker.internal:host-gateway`.

Until that lands, `grok mcp list` smoke works; live `rle__*` calls from the
container will not connect.

### Build + smoke (no RimWorld; CI-safe)

```bash
docker build -f docker/Dockerfile -t rle-grok-build:local .
docker run --rm rle-grok-build:local mcp list    # must list rle only
./docker/grok-docker.sh mcp list                 # Unix wrapper
.\docker\grok-docker.cmd mcp list                # Windows wrapper
```

Grok is pinned (`GROK_VERSION=1.0.13`). Docker Desktop may have the CLI
while the **daemon is stopped** — start it before build/run.

### Windows / Docker Desktop caveats

Docker Desktop on Windows returns **exit 125 Access is denied** when the
wrapper bind-mounts harness temp dirs under `%TEMP%` (typically
`AppData\Local\Temp\rle-grok-home-*` for `GROK_HOME` and `Temp\rle-grok-*`
for `--cwd`). `grok-docker.ps1` / `.sh` detect those hostile paths, **skip
the mount** with a warning, and rely on `MCP_URL` plus an empty container
`GROK_HOME` (the entrypoint writes RLE-only `config.toml`).

`--rm` means grok session files do not persist across ticks when the host
temp home is not mounted. Set `GROK_DOCKER_HOME_VOLUME` to a named volume
to persist `/home/grok/.grok` without using `%TEMP%`.

Auth stays `XAI_API_KEY` (or a mounted `auth.json` file). Do not bake
secrets into the image.

### Windows argv JSON (required for `grok-docker.cmd`)

`grok-docker.cmd` launches PowerShell with `%*`. cmd.exe drops quoted and
large `-p` prompts, so the wrapper starts grok with **no args**. The image
entrypoint then defaults to `mcp list` and exits 0 — Crashlanded ticks
report **0 tokens / 0 actions / success**.

When `binary` is `grok-docker.cmd` / `.ps1` / `.sh`, the harness:

1. Writes the grok argv (everything after the wrapper path) as UTF-8 JSON
2. Sets `RLE_GROK_ARGV_JSON` to that temp file
3. Invokes the wrapper with **no** grok CLI args

Wrappers load that JSON array when the env var is set; otherwise they keep
parsing the command line (manual `.\docker\grok-docker.cmd mcp list` still
works). The JSON file is read on the **host** (not bind-mounted).

`grok-docker.ps1` must invoke docker with **PowerShell splatting**
(`& docker @dockerArgs`). Do **not** use `Start-Process -ArgumentList`: it
re-joins argv into one Windows command line and the CRT re-splits it.
Live prove: the Unicode em dash in `RLE turn — tick 0...` became a new
argument (`error: unexpected argument '—' found`). Optional
`RLE_GROK_DOCKER_TRACE=1` (or a file path) logs one argv element per line
with `-p` redacted — it does not change the docker invoke.

### Host cal (after McpHost PR)

Harness + scenario loop stay on Windows. Point `binary` at the wrapper:

```powershell
$env:XAI_API_KEY = "xai-..."
python scripts/run_scenario.py crashlanded --harness grok-build --ticks 1 `
  --harness-opt "binary=.\docker\grok-docker.cmd" `
  --harness-opt "mcp_advertise_url=http://host.docker.internal:8766/mcp"
```

Details, auth mounts, and the do-not-mount list: [docker/README.md](docker/README.md).

## Warm persist (OpenCode-parity spike)

Jason + CoS diagnosed the OpenCode (~0.82) vs Grok Build (~0.72/0.53) gap as **harness
lifecycle**, not xAI API speed: OpenCode spawns `opencode serve` once and POSTs each
tick; this harness used to `docker run --rm` / `grok -p` **every** tick (cold boot +
MCP + tool surface).

Grok Build has no OpenCode-style HTTP serve. Documented long-lived modes are ACP:

```bash
grok agent --always-approve serve --bind 127.0.0.1:2419 --secret <token>
grok agent --always-approve --model grok-4.6 stdio
```

This spike does **not** implement an ACP client. `warm=true` keeps one stock-Linux-grok
container up and `docker exec`s documented `grok -p --resume` (RLE-only MCP, isolated
`GROK_HOME`). Cold-start remains the default.

### Crashlanded rematch (seed 42 / scoring 1.2 / 300s turns)

From an RLE checkout with RimWorld + RIMAPI on the host and the McpHost bind PR:

```powershell
$env:XAI_API_KEY = "xai-..."
python scripts/run_scenario.py crashlanded --harness grok-build --model grok-4.6 `
  --seed 42 --scoring 1.2 --ticks 10 --tick-interval 30 `
  --harness-opt "binary=.\docker\grok-docker.cmd" `
  --harness-opt warm=true `
  --harness-opt turn_timeout_s=300 `
  --harness-opt mcp_container_reachable=true `
  --harness-opt mcp_advertise_url=http://host.docker.internal:8766/mcp
```

Unix (same knobs; `persistent=true` is an alias for `warm`):

```bash
python scripts/run_scenario.py crashlanded --harness grok-build --model grok-4.6 \
  --seed 42 --scoring 1.2 --ticks 10 --tick-interval 30 \
  --harness-opt binary=./docker/grok-docker.sh \
  --harness-opt persistent=true \
  --harness-opt turn_timeout_s=300 \
  --harness-opt mcp_container_reachable=true \
  --harness-opt mcp_advertise_url=http://host.docker.internal:8766/mcp
```

Windows `.cmd` still uses `RLE_GROK_ARGV_JSON`. Optional `GROK_DOCKER_HOME_VOLUME`
still mounts a named volume on persist **start** (container disk also keeps
`/home/grok/.grok` because the sidecar is not `--rm`).

### How to measure TTFA

Compare the same seed/scoring/timeout with and without `warm=true`:

1. **Tick latency** — `deliberation_log[].latency_ms` / `extras.latency_ms` (prompt → agent return).
2. **Time to first RLE action (TTFA)** — wall clock from turn start to the first `rle__*`
   ledger/tool event (not model TTFT). Cold docker median was ~123s to first action;
   bare `grok -p` ~5–10s; direct xAI tiny call ~0.7s.
3. Tick 0 vs later ticks: persist should drop per-tick `docker run` boot; if TTFA stays
   huge, remaining cost is `grok -p` MCP/tool init — use `acp=true` for process-level
   parity with OpenCode.

## ACP agent serve (OpenCode parity)

OpenCode keeps one `opencode serve` and POSTs each tick (~15s TTFA). Warm persist
(#7) keeps the Docker container but still runs `grok -p` per tick (tick-0 TTFA
still tens of seconds). Documented Grok long-lived mode is ACP:

```bash
grok agent --always-approve serve --bind 127.0.0.1:2419 --secret <token>
grok agent --always-approve --model grok-4.6 stdio
```

This harness uses the **HTTP / WebSocket bind** path (not stdio):

1. `start_agent` spawns `grok agent --always-approve serve --bind … --secret …`
   (host process, or `acp-serve` as PID 1 inside the persist container).
2. Client connects to `ws://127.0.0.1:<port>/ws` with `Authorization: Bearer`
   and `?server-key=` (same auth as xai-org/grok-build `server.rs`).
3. Once: ACP `initialize` then `session/new` (`cwd`, `_meta.yoloMode`, empty
   `mcpServers` — RLE MCP still comes from isolated `GROK_HOME` `config.toml`).
4. Each tick: ACP `session/prompt` with the turn text; wait for the JSON-RPC
   result (`stopReason`: `end_turn` / `max_turn_requests` / `cancelled` / …)
   and `session/update` chunks. `turn_timeout_s` still applies; timeout sends
   `session/cancel` (does **not** kill the serve process).
5. Teardown: `session/close` when advertised, then stop serve / `docker stop`.

Default remains today's `-p` / warm path. `acp=true` on a grok-docker wrapper
implies a persist container (serve must stay up). Windows still uses
`RLE_GROK_ARGV_JSON` for wrapper start/healthcheck; tick prompts go over
WebSocket, not `docker exec` argv.

### Enable + Crashlanded rematch (RLE bot)

`--harness-opt acp=true` (or `mode=acp`). Seed **42** / scoring **1.2** /
`turn_timeout_s=300`:

```powershell
$env:XAI_API_KEY = "xai-..."
python scripts/run_scenario.py crashlanded --harness grok-build --model grok-4.6 `
  --seed 42 --scoring 1.2 --ticks 10 --tick-interval 30 `
  --harness-opt "binary=.\docker\grok-docker.cmd" `
  --harness-opt acp=true `
  --harness-opt turn_timeout_s=300 `
  --harness-opt mcp_container_reachable=true `
  --harness-opt mcp_advertise_url=http://host.docker.internal:8766/mcp
```

Unix:

```bash
python scripts/run_scenario.py crashlanded --harness grok-build --model grok-4.6 \
  --seed 42 --scoring 1.2 --ticks 10 --tick-interval 30 \
  --harness-opt binary=./docker/grok-docker.sh \
  --harness-opt acp=true \
  --harness-opt turn_timeout_s=300 \
  --harness-opt mcp_container_reachable=true \
  --harness-opt mcp_advertise_url=http://host.docker.internal:8766/mcp
```

Same knobs as warm: `binary`, `max_turns`, `disallowed_tools`, `XAI_API_KEY`,
`mcp_advertise_url`, `mcp_container_reachable`, `GROK_DOCKER_HOME_VOLUME`.

### How to measure TTFA (ACP vs warm vs cold)

Same seed/scoring/timeout; compare `acp=true` vs `warm=true` vs default:

1. **Tick latency** — `deliberation_log[].latency_ms` / `extras.latency_ms`.
2. **TTFA** — wall clock from turn start to the first `rle__*` ledger/tool
   event (not model TTFT). OpenCode reference ~15s; warm docker tick 0 was ~75s.
3. Tick 0 vs later ticks. ACP should keep MCP/tool surface warm across ticks
   the way OpenCode's serve process does. `extras.stop_reason` and
   `extras.acp=true` mark ACP turns.

## How it works

- A temp working directory with `.grok/config.toml` declaring the RLE MCP server
  (hosted in-process by RLE over streamable HTTP) as `[mcp_servers.rle] url = …`.
- Cold (default): `grok -p <prompt> --output-format json --yolo --cwd <workdir> --no-subagents
  --no-plan [-m model] [--resume sid] …` (or `docker run --rm` via the wrapper).
- Warm: `docker run -d --name rle-grok-…` once, then `docker exec … /entrypoint.sh grok -p …`
  each tick; `sessionId`, `usage` and `total_cost_usd` from the JSON object feed RLE's tracking.
- ACP: one `grok agent serve` WebSocket; each tick is `session/prompt` until `stopReason`.
- `--smoke-test` needs no Grok Build: a scripted agent plays the same MCP round trip.

## Development

```bash
uv pip install "rimworld-learning-environment[mcp] @ git+https://github.com/AppSprout-dev/RLE"
uv pip install -e ".[dev]"
pytest && ruff check src tests && mypy src
```

MIT (this package). Grok Build is Apache-2.0, xAI.
