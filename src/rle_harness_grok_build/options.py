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
        default=None, ge=1,
        description="Cap on agentic rounds per tick (--max-turns).",
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
        default_factory=list, description="Additional raw flags appended to every invocation.",
    )
