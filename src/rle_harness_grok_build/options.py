"""Options for the Grok Build harness (``--harness-opt key=value``)."""

from __future__ import annotations

from pydantic import Field
from rle.harness.cli_base import HeadlessCliOptions


class GrokBuildOptions(HeadlessCliOptions):
    binary: str = Field(default="grok", description="Grok Build executable (name or path).")
    resume_session: bool = Field(
        default=True,
        description="Resume the same headless session every tick (context carries over).",
    )
    max_turns: int | None = Field(
        default=20, ge=1,
        description=(
            "Cap on agentic rounds per tick (--max-turns). Default 20 so a lost "
            "tool-search loop cannot burn the full turn timeout with zero RLE actions."
        ),
    )
    reasoning_effort: str | None = Field(
        default=None, description="Passed as --reasoning-effort when set.",
    )
    disallowed_tools: list[str] = Field(
        default_factory=lambda: [
            "run_terminal_cmd", "search_replace", "write_file", "delete_file",
            "web_search", "web_fetch",
        ],
        description=(
            "Built-in tools removed so the agent can only act through the RLE MCP tools "
            "(--disallowed-tools). Empty list = leave Grok's defaults."
        ),
    )
    api_key_env: str = Field(
        default="XAI_API_KEY",
        description="Env var holding the xAI API key for headless auth (or use cached login).",
    )
    extra_args: list[str] = Field(
        default_factory=list,
        description=(
            "Additional raw flags appended to every invocation. "
            "--no-subagents / --no-plan are stripped on grok agent serve "
            "(host ACP and docker acp-serve); they remain on grok -p only."
        ),
    )
    mcp_advertise_url: str | None = Field(
        default=None,
        description=(
            "URL written into grok config.toml instead of the in-process bind URL. "
            "For stock grok-in-Docker use http://host.docker.internal:8766/mcp "
            "(requires RLE McpHost bound on 0.0.0.0:8766 — sibling RLE PR)."
        ),
    )
    warm: bool = Field(
        default=False,
        description=(
            "OpenCode-parity lifecycle: start one grok-docker container in setup, "
            "docker exec grok -p --resume each tick, stop in teardown. Default "
            "false keeps today's docker run --rm (or host grok -p) per tick. "
            "No-op for a non-wrapper binary (isolated GROK_HOME + --resume only)."
        ),
    )
    persistent: bool = Field(
        default=False,
        description="Alias for warm. Either --harness-opt enables the persist path.",
    )
    acp: bool = Field(
        default=False,
        description=(
            "Long-lived grok agent serve over ACP (JSON-RPC WebSocket). "
            "Default false keeps today's grok -p / warm docker-exec path. "
            "Also enabled by --harness-opt mode=acp."
        ),
    )
    mode: str | None = Field(
        default=None,
        description="Optional path selector. mode=acp is an alias for acp=true.",
    )
    acp_bind: str | None = Field(
        default=None,
        description=(
            "Host listen address for grok agent serve, host:port. "
            "Default 127.0.0.1 plus an ephemeral port. Inside Docker the agent "
            "binds 0.0.0.0:2419 and this host:port is published."
        ),
    )
    acp_secret: str | None = Field(
        default=None,
        description=(
            "Shared secret for grok agent serve (Authorization Bearer / "
            "?server-key=). Generated per run when omitted."
        ),
    )

    @property
    def warm_enabled(self) -> bool:
        return self.warm or self.persistent

    @property
    def acp_enabled(self) -> bool:
        if self.acp:
            return True
        mode = (self.mode or "").strip().lower()
        return mode == "acp"
