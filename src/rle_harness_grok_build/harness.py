"""Grok Build driven one headless invocation per tick.

Surface used (grok-build ``docs/user-guide/14-headless-mode.md`` and
``07-mcp-servers.md``):

* ``grok -p "<prompt>" --output-format json --yolo --cwd <workdir> [-m MODEL]
  [--resume <sessionId>] [--max-turns N] [--disallowed-tools ...]``
  -> one JSON object: ``text``, ``sessionId``, ``usage{input_tokens,
  output_tokens, reasoning_tokens,...}``, ``total_cost_usd`` (when complete)
* Isolated ``GROK_HOME`` (temp) with RLE-only MCP; Claude/Cursor compatibility
  MCP imports are explicitly disabled so the user's global MCP zoo cannot drown
  out ``rle__*`` tools::

      [mcp_servers.rle]
      url = "http://127.0.0.1:PORT/mcp"
      startup_timeout_sec = 30
      headers = { "x-mcp-session-id" = "{{session_id}}" }

  Tools are namespaced ``rle__<tool>`` (``rle__get_brief``, ``rle__end_turn``).
* Auth is copied from the real ``~/.grok`` (``auth.json``, optional
  ``mcp_credentials.json``) into the isolated home.
* Exit codes: 0 ok, 1 error, 130/143 interrupted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
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

# Auth files to copy from the real ~/.grok into the isolated GROK_HOME.
_AUTH_FILENAMES = ("auth.json", "mcp_credentials.json")

TOOL_NAMING_NOTE = (
    "In this environment the RLE tools are namespaced by server: call rle__get_brief, "
    "rle__work_priority, rle__blueprint, ..., and finish with rle__end_turn. "
    "The only MCP server available is rle --- do not search for other tools."
)


def mcp_config_toml(mcp_url: str) -> str:
    """RLE-only MCP config with compatibility MCP imports disabled."""
    return (
        f"[mcp_servers.{MCP_SERVER_NAME}]\n"
        f'url = "{mcp_url}"\n'
        f"startup_timeout_sec = 30\n"
        f'headers = {{ "x-mcp-session-id" = "{{{{session_id}}}}" }}\n'
        "\n"
        "[compat.claude]\n"
        "mcps = false\n"
        "\n"
        "[compat.cursor]\n"
        "mcps = false\n"
    )


def _real_grok_home() -> Path:
    """User's actual Grok home (ignores a leftover GROK_HOME from a prior run)."""
    return Path.home() / ".grok"


def _copy_auth_into(dest: Path) -> None:
    src = _real_grok_home()
    if not src.is_dir():
        logger.warning("no %s --- headless auth may fail without XAI_API_KEY", src)
        return
    for name in _AUTH_FILENAMES:
        path = src / name
        if path.is_file():
            shutil.copy2(path, dest / name)
            logger.debug("copied %s into isolated GROK_HOME", name)


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
        self._grok_home: Path | None = None
        self._prev_grok_home: str | None = None
        self._session_id: str | None = None
        self._proc: asyncio.subprocess.Process | None = None

    async def _healthcheck_mcp(self) -> None:
        """Verify the isolated config exposes only the RLE MCP server."""
        assert self._binary is not None and self._workdir is not None
        proc = await asyncio.create_subprocess_exec(
            self._binary, "mcp", "list", cwd=self._workdir,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=self._subprocess_env(),
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=20)
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise HarnessStepError("grok MCP healthcheck timed out after 20s") from exc
        output = (stdout_b + stderr_b).decode("utf-8", errors="replace")
        if proc.returncode != 0:
            raise HarnessStepError(
                f"grok MCP healthcheck failed ({proc.returncode}): {output.strip()[-800:]}",
            )
        if not any(re.search(r"\brle\b", line, re.IGNORECASE) for line in output.splitlines()):
            raise HarnessStepError(
                f"grok MCP healthcheck did not list rle: {output.strip()[-800:]}",
            )
        compat = [
            name for name in ("wandb", "claude", "cursor")
            if re.search(rf"\b{name}\b", output, re.IGNORECASE)
        ]
        if compat:
            raise HarnessStepError(
                "grok MCP healthcheck found unexpected compatibility MCPs: "
                + ", ".join(compat),
            )
        logger.info("grok MCP healthcheck passed: rle is available")

    async def start_agent(self, mcp_url: str) -> None:
        binary = shutil.which(self.opts.binary)
        if binary is None:
            raise HarnessStepError(f"Grok Build binary {self.opts.binary!r} not found on PATH")
        self._binary = binary
        self._workdir = tempfile.mkdtemp(prefix="rle-grok-")
        # Isolate from the user's ~/.grok MCP zoo (wandb, stripe, HF, ...).
        # Without this, grok burns the turn tool-searching and never hits rle__*.
        self._grok_home = Path(tempfile.mkdtemp(prefix="rle-grok-home-"))
        self._prev_grok_home = os.environ.get("GROK_HOME")
        os.environ["GROK_HOME"] = str(self._grok_home)
        _copy_auth_into(self._grok_home)
        cfg_text = mcp_config_toml(mcp_url)
        (self._grok_home / "config.toml").write_text(cfg_text, encoding="utf-8")
        # Project-scoped copy too (cwd priority / defense in depth).
        cfg_dir = Path(self._workdir) / ".grok"
        cfg_dir.mkdir()
        (cfg_dir / "config.toml").write_text(cfg_text, encoding="utf-8")
        logger.info(
            "isolated GROK_HOME=%s with RLE-only MCP at %s",
            self._grok_home, mcp_url,
        )
        await self._healthcheck_mcp()
        if not os.environ.get(self.opts.api_key_env):
            logger.info(
                "%s not set --- relying on Grok Build's cached login for headless auth",
                self.opts.api_key_env,
            )

    def render_prompt(self, brief: Any) -> str:
        return super().render_prompt(brief) + "\n\n" + TOOL_NAMING_NOTE

    def _subprocess_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self._grok_home is not None:
            env["GROK_HOME"] = str(self._grok_home)
        return env

    async def send_turn(self, prompt: str) -> TurnResult:
        assert self._binary is not None and self._workdir is not None
        cmd = build_command(
            self._binary, prompt, self.opts,
            model=self.opts.model or self.ctx.config.model,
            workdir=self._workdir, session_id=self._session_id,
        )
        logger.debug("grok invocation: %s", " ".join(cmd[:1] + ["-p", "<prompt>"] + cmd[3:]))
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=self._workdir,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=self._subprocess_env(),
        )
        self._proc = proc
        try:
            stdout_b, stderr_b = await proc.communicate()
        except asyncio.CancelledError:
            # communicate() owns the pipe readers; do not call it a second time
            # from abort_turn after cancellation.
            await self._terminate_process(proc)
            raise
        finally:
            if self._proc is proc:
                self._proc = None
        returncode = proc.returncode or 0
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        if stderr.strip():
            logger.debug("grok stderr (tail): %s", stderr.strip()[-1500:])
        if returncode != 0:
            raise HarnessStepError(
                f"grok exited {returncode}: {(stderr or stdout).strip()[-800:]}",
            )
        turn = parse_json_output(stdout)
        sid = turn.extras.get("session_id")
        if sid:
            self._session_id = str(sid)
        return turn

    async def _terminate_process(self, proc: asyncio.subprocess.Process) -> None:
        """Stop a subprocess without a second communicate() on its pipes."""
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass

    async def abort_turn(self) -> None:
        proc = self._proc
        if proc is not None:
            await self._terminate_process(proc)
        if self._proc is proc:
            self._proc = None

    async def stop_agent(self) -> None:
        await self.abort_turn()
        if self._workdir is not None:
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None
        if self._grok_home is not None:
            shutil.rmtree(self._grok_home, ignore_errors=True)
            self._grok_home = None
        if self._prev_grok_home is None:
            os.environ.pop("GROK_HOME", None)
        else:
            os.environ["GROK_HOME"] = self._prev_grok_home
        self._prev_grok_home = None

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
