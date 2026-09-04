# rle-harness-grok-build

[Grok Build](https://github.com/xai-org/grok-build) as an [RLE](https://github.com/AppSprout-dev/RLE) harness.

RLE benchmarks **harnesses × models** on a live RimWorld colony. This package lets xAI's
Grok Build coding agent be the harness: each tick is one headless invocation
(`grok -p … --output-format json`) that resumes the previous session, acts on the colony
through the RLE MCP tools (`rle__get_brief`, `rle__work_priority`, … `rle__end_turn`), and
the writes that reached the game are scored with the same composite as every other harness.

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
| `resume_session` | true | `--resume <sessionId>` every tick so context carries over |
| `max_turns` | – | `--max-turns` cap on agentic rounds per tick |
| `reasoning_effort` | – | `--reasoning-effort` |
| `disallowed_tools` | shell/edit/web tools | Built-ins removed so the agent can only act via RLE tools |
| `turn_timeout_s` | 180 | Kill the invocation after this many seconds |
| `extra_instructions` | – | Appended to every turn prompt |
| `extra_args` | – | Raw flags appended to every invocation |

## How it works

- A temp working directory with `.grok/config.toml` declaring the RLE MCP server
  (hosted in-process by RLE over streamable HTTP) as `[mcp_servers.rle] url = …`.
- Per tick: `grok -p <prompt> --output-format json --yolo --cwd <workdir> --no-subagents
  --no-plan [-m model] [--resume sid] …`; `sessionId`, `usage` and `total_cost_usd` from the
  JSON object feed RLE's tracking.
- `--smoke-test` needs no Grok Build: a scripted agent plays the same MCP round trip.

## Development

```bash
uv pip install "rimworld-learning-environment[mcp] @ git+https://github.com/AppSprout-dev/RLE"
uv pip install -e ".[dev]"
pytest && ruff check src tests && mypy src
```

MIT (this package). Grok Build is Apache-2.0, xAI.
