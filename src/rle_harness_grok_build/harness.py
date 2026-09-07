"""Grok Build driven one headless invocation per tick (optional warm / ACP).

Default (cold): one ``grok -p`` or ``docker run --rm`` per tick.

Warm (``--harness-opt warm=true`` / ``persistent=true`` + grok-docker wrapper):
one sidecar container started in ``start_agent``, ``docker exec`` each tick
with ``--resume``, stopped in teardown.

ACP (``--harness-opt acp=true`` / ``mode=acp``): long-lived
``grok agent --always-approve serve --bind … --secret …``. Each tick is an
ACP ``session/prompt`` over the documented WebSocket (``ws://bind/ws``).
Docker prefers serve inside the warm persist container (entrypoint
``acp-serve``). Default stays today's ``-p`` / warm path.

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
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, ClassVar

from rle.harness import HarnessStepError
from rle.harness.cli_base import HeadlessCliHarness, TurnResult

from rle_harness_grok_build.acp import (
    ACP_CONTAINER_PORT,
    CONTAINER_WORKDIR,
    DEFAULT_READY_TIMEOUT_S,
    AcpClient,
    AcpError,
    acp_ws_url,
    agent_option_flags,
    build_agent_serve_command,
    client_ws_host,
    resolve_acp_listen,
)
from rle_harness_grok_build.argv_json import (
    ARGV_JSON_ENV,
    is_docker_wrapper_binary,
    prepare_docker_wrapper_invocation,
)
from rle_harness_grok_build.isolated_home import (
    AUTH_FILENAMES,
    check_mcp_list_output,
    effective_mcp_url,
    mcp_config_toml,
    resolve_binary,
    write_project_grok_config,
)
from rle_harness_grok_build.options import GrokBuildOptions
from rle_harness_grok_build.persist import (
    PersistAction,
    apply_acp_env,
    apply_persist_env,
    new_container_name,
    persist_start_args,
)

logger = logging.getLogger(__name__)

# Re-exported for callers/tests that imported these from harness.
__all__ = (
    "AUTH_FILENAMES",
    "GrokBuildHarness",
    "TOOL_NAMING_NOTE",
    "binary_version",
    "build_command",
    "mcp_config_toml",
    "parse_json_output",
)

# Auth files to copy from the real ~/.grok into the isolated GROK_HOME.
_AUTH_FILENAMES = AUTH_FILENAMES

TOOL_NAMING_NOTE = (
    "In this environment the RLE tools are namespaced by server: call rle__get_brief, "
    "rle__work_priority, rle__blueprint, ..., and finish with rle__end_turn. "
    "The only MCP server available is rle --- do not search for other tools."
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
        # Headless ``grok -p`` only. Never add these to ``grok agent serve``
        # (pinned CLI: unexpected argument, exit 2).
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
        self._mcp_url: str | None = None
        self._persist_container: str | None = None
        self._acp: AcpClient | None = None
        self._serve_proc: asyncio.subprocess.Process | None = None
        self._serve_log_tasks: list[asyncio.Task[None]] = []
        self._acp_publish: str | None = None
        self._acp_secret: str | None = None
        self._acp_session_cwd: str | None = None

    async def _healthcheck_mcp(self) -> None:
        """Verify the isolated config exposes only the RLE MCP server."""
        assert self._binary is not None and self._workdir is not None
        try:
            returncode, stdout, stderr = await asyncio.wait_for(
                self._invoke([self._binary, "mcp", "list"]),
                timeout=20,
            )
        except asyncio.TimeoutError as exc:
            await self.abort_turn()
            raise HarnessStepError("grok MCP healthcheck timed out after 20s") from exc
        output = stdout + stderr
        if returncode != 0:
            raise HarnessStepError(
                f"grok MCP healthcheck failed ({returncode}): {output.strip()[-800:]}",
            )
        try:
            check_mcp_list_output(output)
        except ValueError as exc:
            raise HarnessStepError(str(exc)) from exc
        logger.info("grok MCP healthcheck passed: rle is available")

    async def start_agent(self, mcp_url: str) -> None:
        binary = resolve_binary(self.opts.binary)
        if binary is None:
            raise HarnessStepError(f"Grok Build binary {self.opts.binary!r} not found on PATH")
        self._binary = binary
        self._workdir = tempfile.mkdtemp(prefix="rle-grok-")
        # Isolate from the user's ~/.grok MCP zoo (wandb, stripe, HF, ...).
        # Without this, grok burns the turn tool-searching and never hits rle__*.
        self._grok_home = Path(tempfile.mkdtemp(prefix="rle-grok-home-"))
        self._prev_grok_home = os.environ.get("GROK_HOME")
        os.environ["GROK_HOME"] = str(self._grok_home)
        cfg_url = effective_mcp_url(mcp_url, self.opts.mcp_advertise_url)
        self._mcp_url = cfg_url
        _copy_auth_into(self._grok_home)
        (self._grok_home / "config.toml").write_text(mcp_config_toml(cfg_url), encoding="utf-8")
        # Project-scoped copy too (cwd priority / defense in depth).
        write_project_grok_config(Path(self._workdir), cfg_url)
        logger.info(
            "isolated GROK_HOME=%s with RLE-only MCP at %s",
            self._grok_home, cfg_url,
        )
        if self.opts.acp_enabled and is_docker_wrapper_binary(binary):
            self._persist_container = new_container_name()
            logger.info(
                "ACP serve: persist container %s runs grok agent serve (not grok -p)",
                self._persist_container,
            )
        elif self.opts.acp_enabled:
            logger.info("ACP serve: host grok agent --always-approve serve")
        elif self.opts.warm_enabled and is_docker_wrapper_binary(binary):
            self._persist_container = new_container_name()
            logger.info(
                "warm persist: starting container %s (docker exec per tick, not --rm)",
                self._persist_container,
            )
        elif self.opts.warm_enabled:
            logger.info(
                "warm=true without a grok-docker wrapper: isolated GROK_HOME + "
                "--resume only (no long-lived container). Use acp=true for "
                "documented grok agent serve.",
            )
        try:
            if self.opts.acp_enabled:
                await self._start_acp_agent()
            elif self._persist_container is not None:
                await self._start_persist_container()
                await self._healthcheck_mcp()
            else:
                await self._healthcheck_mcp()
        except Exception:
            await self._stop_acp()
            await self._stop_persist_container()
            raise
        if not os.environ.get(self.opts.api_key_env):
            logger.info(
                "%s not set --- relying on Grok Build's cached login for headless auth",
                self.opts.api_key_env,
            )

    def render_prompt(self, brief: Any) -> str:
        return super().render_prompt(brief) + "\n\n" + TOOL_NAMING_NOTE

    def _subprocess_env(self) -> dict[str, str]:
        env = os.environ.copy()
        # A leftover sidecar path from a prior/manual run must not leak into children.
        env.pop(ARGV_JSON_ENV, None)
        if self._grok_home is not None:
            env["GROK_HOME"] = str(self._grok_home)
        if self._mcp_url is not None:
            env["MCP_URL"] = self._mcp_url
        return env

    def _prepare_exec(
        self,
        cmd: list[str],
        *,
        persist_action: PersistAction | None = None,
    ) -> tuple[list[str], dict[str, str], Path | None]:
        """Build subprocess argv/env; grok-docker wrappers get an argv JSON sidecar."""
        env = self._subprocess_env()
        action = persist_action
        if action is None and self._persist_container is not None:
            action = "exec"
        if action is not None and self._persist_container is not None:
            apply_persist_env(env, container=self._persist_container, action=action)
        if (
            action == "start"
            and self._acp_publish is not None
            and self._acp_secret is not None
        ):
            apply_acp_env(env, publish=self._acp_publish, secret=self._acp_secret)
        invoke, sidecar = prepare_docker_wrapper_invocation(cmd, env)
        return invoke, env, sidecar

    async def _invoke(
        self,
        cmd: list[str],
        *,
        persist_action: PersistAction | None = None,
    ) -> tuple[int, str, str]:
        """Run *cmd* (or the docker wrapper + sidecar) and return rc/stdout/stderr."""
        assert self._workdir is not None
        invoke, env, sidecar = self._prepare_exec(cmd, persist_action=persist_action)
        try:
            proc = await asyncio.create_subprocess_exec(
                *invoke, cwd=self._workdir,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=env,
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
        finally:
            if sidecar is not None:
                sidecar.unlink(missing_ok=True)
        return (
            proc.returncode or 0,
            stdout_b.decode("utf-8", errors="replace"),
            stderr_b.decode("utf-8", errors="replace"),
        )

    def _agent_model(self) -> str | None:
        return self.opts.model or self.ctx.config.model

    async def _start_persist_container(self) -> None:
        assert self._binary is not None and self._workdir is not None
        flags = None
        if self.opts.acp_enabled:
            flags = agent_option_flags(self.opts, model=self._agent_model())
        returncode, stdout, stderr = await self._invoke(
            persist_start_args(
                self._binary, self._workdir,
                acp=self.opts.acp_enabled, agent_flags=flags,
            ),
            persist_action="start",
        )
        if returncode != 0:
            raise HarnessStepError(
                f"warm persist start failed ({returncode}): "
                f"{(stderr or stdout).strip()[-800:]}",
            )

    async def _start_acp_agent(self) -> None:
        """Spawn documented grok agent serve and open one ACP session."""
        assert self._binary is not None and self._workdir is not None
        host, port = resolve_acp_listen(self.opts.acp_bind)
        secret = self.opts.acp_secret or secrets.token_urlsafe(32)
        self._acp_secret = secret
        model = self._agent_model()
        if self._persist_container is not None:
            self._acp_publish = f"{host}:{port}:{ACP_CONTAINER_PORT}"
            self._acp_session_cwd = CONTAINER_WORKDIR
            await self._start_persist_container()
            await self._healthcheck_mcp()
            connect_host, connect_port = client_ws_host(host), port
        else:
            await self._healthcheck_mcp()
            bind = f"{host}:{port}"
            cmd = build_agent_serve_command(
                self._binary, bind=bind, secret=secret,
                cwd=self._workdir, model=model, opts=self.opts,
            )
            await self._spawn_host_serve(cmd)
            self._acp_session_cwd = self._workdir
            connect_host, connect_port = client_ws_host(host), port
        client = AcpClient(acp_ws_url(connect_host, connect_port, secret=secret), secret)
        try:
            await client.connect(timeout_s=DEFAULT_READY_TIMEOUT_S)
            await client.initialize()
            sid = await client.new_session(self._acp_session_cwd)
        except AcpError as exc:
            await client.close()
            raise HarnessStepError(str(exc)) from exc
        self._acp = client
        self._session_id = sid
        logger.info("ACP session %s ready (cwd=%s)", sid, self._acp_session_cwd)

    async def _spawn_host_serve(self, cmd: list[str]) -> None:
        assert self._workdir is not None
        invoke, env, sidecar = self._prepare_exec(cmd)
        try:
            proc = await asyncio.create_subprocess_exec(
                *invoke, cwd=self._workdir,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        finally:
            if sidecar is not None:
                sidecar.unlink(missing_ok=True)
        self._serve_proc = proc
        if proc.stdout is not None:
            self._serve_log_tasks.append(
                asyncio.create_task(self._drain_stream(proc.stdout, "stdout")),
            )
        if proc.stderr is not None:
            self._serve_log_tasks.append(
                asyncio.create_task(self._drain_stream(proc.stderr, "stderr")),
            )
        await asyncio.sleep(0.05)
        if proc.returncode is not None:
            raise HarnessStepError(
                f"grok agent serve exited {proc.returncode} during startup",
            )

    async def _drain_stream(
        self, stream: asyncio.StreamReader, label: str,
    ) -> None:
        try:
            while True:
                line = await stream.readline()
                if not line:
                    return
                logger.debug(
                    "grok agent serve %s: %s",
                    label, line.decode("utf-8", errors="replace").rstrip(),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("grok agent serve %s drain failed", label, exc_info=True)

    async def _stop_acp(self) -> None:
        client = self._acp
        self._acp = None
        if client is not None:
            try:
                await client.close()
            except Exception:
                logger.debug("ACP client close failed", exc_info=True)
        proc = self._serve_proc
        self._serve_proc = None
        if proc is not None:
            await self._terminate_process(proc)
        for task in self._serve_log_tasks:
            task.cancel()
        self._serve_log_tasks = []
        self._acp_publish = None
        self._acp_secret = None
        self._acp_session_cwd = None

    async def _stop_persist_container(self) -> None:
        if self._persist_container is None or self._binary is None:
            self._persist_container = None
            return
        try:
            if self._workdir is not None:
                await self._invoke([self._binary], persist_action="stop")
        except Exception:
            logger.debug("warm persist stop failed", exc_info=True)
        self._persist_container = None

    async def send_turn(self, prompt: str) -> TurnResult:
        if self._acp is not None:
            if not self.opts.resume_session and self._acp_session_cwd:
                try:
                    sid = await self._acp.new_session(self._acp_session_cwd)
                except AcpError as exc:
                    raise HarnessStepError(str(exc)) from exc
                self._session_id = sid
            try:
                turn = await self._acp.prompt(prompt)
            except AcpError as exc:
                raise HarnessStepError(str(exc)) from exc
            extra_sid = turn.extras.get("session_id")
            if extra_sid:
                self._session_id = str(extra_sid)
            return turn
        assert self._binary is not None and self._workdir is not None
        cmd = build_command(
            self._binary, prompt, self.opts,
            model=self.opts.model or self.ctx.config.model,
            workdir=self._workdir, session_id=self._session_id,
        )
        logger.debug("grok invocation: %s", " ".join(cmd[:1] + ["-p", "<prompt>"] + cmd[3:]))
        returncode, stdout, stderr = await self._invoke(cmd)
        if stderr.strip():
            logger.debug("grok stderr (tail): %s", stderr.strip()[-1500:])
        if returncode != 0:
            raise HarnessStepError(
                f"grok exited {returncode}: {(stderr or stdout).strip()[-800:]}",
            )
        turn = parse_json_output(stdout)
        extra_sid = turn.extras.get("session_id")
        if extra_sid:
            self._session_id = str(extra_sid)
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
        if self._acp is not None:
            try:
                await self._acp.cancel()
            except Exception:
                logger.debug("ACP session/cancel failed", exc_info=True)
            return
        proc = self._proc
        if proc is not None:
            await self._terminate_process(proc)
        if self._proc is proc:
            self._proc = None

    async def stop_agent(self) -> None:
        await self.abort_turn()
        await self._stop_acp()
        await self._stop_persist_container()
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
    # resolve_binary finds absolute .cmd/.ps1/.sh wrappers when shutil.which misses them.
    path = resolve_binary(binary)
    if path is None:
        return "not installed"
    env = os.environ.copy()
    env.pop(ARGV_JSON_ENV, None)
    sidecar = None
    try:
        invoke, sidecar = prepare_docker_wrapper_invocation([path, "--version"], env)
        out = subprocess.run(
            invoke,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    finally:
        if sidecar is not None:
            sidecar.unlink(missing_ok=True)
    text = (out.stdout or out.stderr).strip()
    return text.splitlines()[0] if text else "unknown"
