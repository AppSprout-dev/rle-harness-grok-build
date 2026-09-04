"""Grok Build driven one headless invocation per tick.

Surface used (grok-build ``docs/user-guide/14-headless-mode.md`` and
``07-mcp-servers.md``):

* ``grok -p "<prompt>" --output-format json --yolo --cwd <workdir> [-m MODEL]
  [--resume <sessionId>] [--max-turns N] [--disallowed-tools ...]``
  -> one JSON object: ``text``, ``sessionId``, ``usage{input_tokens,
  output_tokens, reasoning_tokens,...}``, ``total_cost_usd`` (when complete)
* Project-scoped MCP config ``<workdir>/.grok/config.toml``::

      [mcp_servers.rle]
      url = "http://127.0.0.1:PORT/mcp"

  Tools are namespaced ``rle__<tool>`` (``rle__get_brief``, ``rle__end_turn``).
* Exit codes: 0 ok, 1 error, 130/143 interrupted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, ClassVar

from rle.harness import HarnessStepError
from rle.harness.cli_base import HeadlessCliHarness, TurnResult

from rle_harness_grok_build.options import GrokBuildOptions

logger = logging.getLogger(__name__)

MCP_SERVER_NAME = "rle"

TOOL_NAMING_NOTE = (
    "In this environment the RLE tools are namespaced by server: call rle__get_brief, "
    "rle__work_priority, rle__blueprint, ..., and finish with rle__end_turn."
)


def mcp_config_toml(mcp_url: str) -> str:
    return f'[mcp_servers.{MCP_SERVER_NAME}]\nurl = "{mcp_url}"\n'


def build_command(
    binary: str, prompt: str, opts: GrokBuildOptions, *, model: str | None,
    workdir: str, session_id: str | None,
) -> list[str]:
    cmd = [
        binary, "-p", prompt,
        "--output-format", "json",
        "--yolo",
        "--cwd", workdir,
        "--no-subagents",
        "--no-plan",
    ]
    if model:
        cmd += ["-m", model]
    if session_id and opts.resume_session:
        cmd += ["--resume", session_id]
    if opts.max_turns:
        cmd += ["--max-turns", str(opts.max_turns)]
    if opts.reasoning_effort:
        cmd += ["--reasoning-effort", opts.reasoning_effort]
    if opts.disallowed_tools:
        cmd += ["--disallowed-tools", ",".join(opts.disallowed_tools)]
    cmd += list(opts.extra_args)
    return cmd


def parse_json_output(stdout: str) -> TurnResult:
    """Turn the headless ``json`` object into a TurnResult (tolerant of noise)."""
    data: Any = None
    text = stdout.strip()
    # The object is the last JSON document on stdout; tolerate leading log lines.
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    if data is None:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return TurnResult(text=text)
    if not isinstance(data, dict):
        return TurnResult(text=text)
    if data.get("type") == "error":
        raise HarnessStepError(f"grok reported an error: {data.get('message', data)}")
    usage = data.get("usage") or {}
    cached = int(usage.get("cache_read_input_tokens", 0) or 0) + int(
        usage.get("cache_creation_input_tokens", 0) or 0,
    )
    return TurnResult(
        text=str(data.get("text", "")),
        prompt_tokens=int(usage.get("input_tokens", 0) or 0) + cached,
        completion_tokens=int(usage.get("output_tokens", 0) or 0),
        reasoning_tokens=int(usage.get("reasoning_tokens", 0) or 0),
        extras={
            "session_id": str(data.get("sessionId", "")),
            "stop_reason": data.get("stopReason"),
            "num_turns": data.get("num_turns"),
            "cost_usd": data.get("total_cost_usd"),
            "usage_is_incomplete": bool(data.get("usage_is_incomplete", False)),
        },
    )


class GrokBuildHarness(HeadlessCliHarness):
    name: ClassVar[str] = "grok-build"

    def __init__(self, options: GrokBuildOptions) -> None:
        super().__init__(options)
        self.opts = options
        self._binary: str | None = None
        self._workdir: str | None = None
        self._session_id: str | None = None
        self._proc: asyncio.subprocess.Process | None = None

    async def start_agent(self, mcp_url: str) -> None:
        binary = shutil.which(self.opts.binary)
        if binary is None:
            raise HarnessStepError(f"Grok Build binary {self.opts.binary!r} not found on PATH")
        self._binary = binary
        self._workdir = tempfile.mkdtemp(prefix="rle-grok-")
        cfg_dir = Path(self._workdir) / ".grok"
        cfg_dir.mkdir()
        (cfg_dir / "config.toml").write_text(mcp_config_toml(mcp_url))
        if not os.environ.get(self.opts.api_key_env):
            logger.info(
                "%s not set — relying on Grok Build's cached login for headless auth",
                self.opts.api_key_env,
            )

    def render_prompt(self, brief: Any) -> str:
        return super().render_prompt(brief) + "\n\n" + TOOL_NAMING_NOTE

    async def send_turn(self, prompt: str) -> TurnResult:
        assert self._binary is not None and self._workdir is not None
        cmd = build_command(
            self._binary, prompt, self.opts,
            model=self.opts.model or self.ctx.config.model,
            workdir=self._workdir, session_id=self._session_id,
        )
        logger.debug("grok invocation: %s", " ".join(cmd[:1] + ["-p", "<prompt>"] + cmd[3:]))
        self._proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=self._workdir,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await self._proc.communicate()
        except asyncio.CancelledError:
            await self.abort_turn()
            raise
        returncode = self._proc.returncode or 0
        self._proc = None
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        if returncode != 0:
            raise HarnessStepError(
                f"grok exited {returncode}: {(stderr or stdout).strip()[-800:]}",
            )
        turn = parse_json_output(stdout)
        sid = turn.extras.get("session_id")
        if sid:
            self._session_id = str(sid)
        return turn

    async def abort_turn(self) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                proc.kill()
        self._proc = None

    async def stop_agent(self) -> None:
        await self.abort_turn()
        if self._workdir is not None:
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None

    def agent_versions(self) -> dict[str, str]:
        return {"grok-build": binary_version(self.opts.binary)}


def binary_version(binary: str) -> str:
    path = shutil.which(binary)
    if path is None:
        return "not installed"
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    text = (out.stdout or out.stderr).strip()
    return text.splitlines()[0] if text else "unknown"
