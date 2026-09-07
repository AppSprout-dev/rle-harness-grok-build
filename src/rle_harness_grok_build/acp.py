"""ACP client for documented ``grok agent serve`` (JSON-RPC over WebSocket).

Upstream (xai-org/grok-build ``15-agent-mode.md`` + ``server.rs``):

* ``grok agent --always-approve serve --bind 127.0.0.1:2419 --secret <token>``
* Clients connect to ``ws://<bind>/ws``
* Auth: ``Authorization: Bearer <token>`` or ``?server-key=`` (GROK_AGENT_SECRET)

Protocol is Agent Client Protocol (https://agentclientprotocol.com), not a
private RPC. A tick is ``session/prompt``; the turn is done when that
JSON-RPC request returns ``stopReason`` (``end_turn``, ``max_turn_requests``,
``cancelled``, …). Timeout uses ``session/cancel``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import websockets
from rle.harness.cli_base import TurnResult
from websockets.exceptions import ConnectionClosed

from rle_harness_grok_build.cost import (
    parse_cost_usd,
    parse_generation_ids,
    provider_cost_extras,
)
from rle_harness_grok_build.options import GrokBuildOptions

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
CLIENT_NAME = "rle-harness-grok-build"
CLIENT_VERSION = "0.1.0"
ACP_WS_PATH = "/ws"
ACP_CONTAINER_PORT = 2419
ACP_CONTAINER_BIND = f"0.0.0.0:{ACP_CONTAINER_PORT}"
CONTAINER_WORKDIR = "/work"
DEFAULT_READY_TIMEOUT_S = 45.0
_WS_MAX_SIZE = 8 * 1024 * 1024


class AcpError(Exception):
    """ACP transport or JSON-RPC failure."""


def pick_loopback_port() -> int:
    """Ephemeral TCP port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def resolve_acp_listen(explicit: str | None) -> tuple[str, int]:
    """Parse ``host:port`` (port ``0`` = ephemeral). Default ``127.0.0.1:<free>``."""
    if not explicit:
        return "127.0.0.1", pick_loopback_port()
    host, sep, port_s = explicit.rpartition(":")
    if not sep:
        raise ValueError(f"acp_bind must be host:port, got {explicit!r}")
    host = host.strip().strip("[]") or "127.0.0.1"
    port = int(port_s)
    if port == 0:
        port = pick_loopback_port()
    return host, port


def client_ws_host(bind_host: str) -> str:
    """Host the client dials; wildcard binds are reached via loopback."""
    if bind_host in {"0.0.0.0", "::", "[::]"}:
        return "127.0.0.1"
    return bind_host


def acp_ws_url(host: str, port: int, *, secret: str) -> str:
    """``ws://host:port/ws?server-key=`` (grok ``server.rs`` query auth)."""
    query = urlencode({"server-key": secret})
    return urlunsplit(("ws", f"{client_ws_host(host)}:{port}", ACP_WS_PATH, query, ""))


def redact_ws_url(url: str) -> str:
    """Drop ``server-key`` from a URL for logs."""
    parts = urlsplit(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    if "server-key" in query:
        query["server-key"] = ["<redacted>"]
    return urlunsplit((
        parts.scheme, parts.netloc, parts.path,
        urlencode(query, doseq=True), parts.fragment,
    ))


# Headless ``grok -p`` / PagerArgs flags (14-headless-mode.md + this
# harness's ``build_command``). Pinned ``grok agent`` (1.0.13) rejects
# them: unexpected argument, exit 2, persist container dies.
# Documented agent options (15-agent-mode.md AgentArgs) are ``-m/--model``
# and ``--always-approve`` (plus reauth / profile / leader). ``--yolo`` is
# a -p-era alias — serve argv uses ``--always-approve`` only.
# ``--cwd`` is session/new + process cwd, not an agent-serve flag.
_AGENT_SERVE_P_ONLY_SWITCHES = frozenset({
    "--no-subagents",
    "--no-plan",
    "--yolo",
    "-p",
    "--single",
    "--include-partial-messages",
    "--fork-session",
    "--continue",
    "-c",
    "--no-memory",
    "--disable-web-search",
})
_AGENT_SERVE_P_ONLY_VALUE_FLAGS = frozenset({
    "--cwd",
    "--max-turns",
    "--disallowed-tools",
    "--output-format",
    "--resume",
    "-r",
    "--reasoning-effort",
    "--effort",
    "--session-id",
    "-s",
    "--prompt-json",
    "--prompt-file",
    "--permission-mode",
    "--tools",
})
AGENT_SERVE_UNSUPPORTED_FLAGS = (
    _AGENT_SERVE_P_ONLY_SWITCHES | _AGENT_SERVE_P_ONLY_VALUE_FLAGS
)


def without_agent_serve_unsupported_flags(args: Sequence[str]) -> list[str]:
    """Drop headless ``grok -p`` flags (and their values) from agent-serve argv."""
    out: list[str] = []
    skip_value = False
    for arg in args:
        if skip_value:
            skip_value = False
            continue
        if arg in _AGENT_SERVE_P_ONLY_SWITCHES:
            continue
        name = arg.split("=", 1)[0]
        if name in _AGENT_SERVE_P_ONLY_VALUE_FLAGS:
            if "=" not in arg:
                skip_value = True
            continue
        out.append(arg)
    return out


def agent_option_flags(opts: GrokBuildOptions, *, model: str | None) -> list[str]:
    """Flags that belong after ``agent`` and before ``serve`` / ``stdio``.

    Documented serve argv is ``grok agent --always-approve [-m MODEL] serve
    …``. Harness ``max_turns`` / ``disallowed_tools`` / ``reasoning_effort``
    and headless ``extra_args`` are ``grok -p`` only (1.0.13 rejects them).
    """
    flags: list[str] = []
    if model:
        flags += ["-m", model]
    flags += list(opts.extra_args)
    return without_agent_serve_unsupported_flags(flags)


def build_agent_serve_command(
    binary: str,
    *,
    bind: str,
    secret: str,
    cwd: str | None,
    model: str | None,
    opts: GrokBuildOptions,
) -> list[str]:
    """``grok agent --always-approve [-m MODEL] serve --bind … --secret …``.

    *cwd* is accepted for callers but never forwarded: ACP ``session/new``
    and the subprocess working directory own the project path. Headless
    ``grok -p`` flags are stripped even when they arrive via ``extra_args``.
    """
    del cwd  # session/new + process cwd; not a ``grok agent serve`` flag.
    cmd = [binary, "agent", "--always-approve"]
    cmd += agent_option_flags(opts, model=model)
    cmd += ["serve", "--bind", bind, "--secret", secret]
    return without_agent_serve_unsupported_flags(cmd)


@dataclass
class _TurnAccumulator:
    text_parts: list[str] = field(default_factory=list)
    used_tokens: int = 0
    cost_usd: float | None = None
    usage: dict[str, Any] | None = None
    cost: Any = None
    generation_ids: list[str] = field(default_factory=list)

    def absorb_provider_fields(self, payload: Mapping[str, Any] | None) -> None:
        """Merge cost / usage / generation IDs from an ACP update or RPC result."""
        if not isinstance(payload, Mapping):
            return
        parsed = parse_cost_usd(payload)
        if parsed is not None:
            self.cost_usd = parsed
        if "cost" in payload:
            self.cost = payload.get("cost")
        if "usage" in payload and isinstance(payload.get("usage"), dict):
            self.usage = dict(payload["usage"])
        for gen_id in parse_generation_ids(payload):
            if gen_id not in self.generation_ids:
                self.generation_ids.append(gen_id)

    def on_update(self, params: dict[str, Any]) -> None:
        update = params.get("update")
        if not isinstance(update, dict):
            return
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            if isinstance(content, dict):
                text = content.get("text")
                if text:
                    self.text_parts.append(str(text))
            return
        if kind == "usage_update":
            usage = {key: value for key, value in update.items() if key != "sessionUpdate"}
            self.usage = usage
            used = update.get("used")
            if isinstance(used, int):
                self.used_tokens = used
            self.absorb_provider_fields(usage)
            return
        # tool_call / tool_call_update / plan / agent_thought_chunk: ignore.

    def to_turn_result(self, *, stop_reason: str | None, session_id: str) -> TurnResult:
        extras: dict[str, Any] = {
            "session_id": session_id,
            "stop_reason": stop_reason,
            "acp": True,
        }
        extras.update(
            provider_cost_extras(
                cost_usd=self.cost_usd,
                generation_ids=self.generation_ids,
                usage=self.usage,
                cost=self.cost,
            ),
        )
        return TurnResult(
            text="".join(self.text_parts),
            prompt_tokens=self.used_tokens,
            extras=extras,
        )


class AcpClient:
    """JSON-RPC 2.0 ACP client over one long-lived WebSocket."""

    def __init__(self, url: str, secret: str) -> None:
        self._url = url
        self._secret = secret
        self._ws: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._session_id: str | None = None
        self._can_close_session = False
        self._turn: _TurnAccumulator | None = None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def connect(self, *, timeout_s: float = DEFAULT_READY_TIMEOUT_S) -> None:
        deadline = time.monotonic() + timeout_s
        last: Exception | None = None
        safe = redact_ws_url(self._url)
        while time.monotonic() < deadline:
            try:
                self._ws = await websockets.connect(
                    self._url,
                    additional_headers={"Authorization": f"Bearer {self._secret}"},
                    max_size=_WS_MAX_SIZE,
                    open_timeout=5,
                    close_timeout=2,
                    ping_interval=None,
                )
                self._reader = asyncio.create_task(self._read_loop())
                logger.info("ACP WebSocket connected %s", safe)
                return
            except Exception as exc:
                last = exc
                await asyncio.sleep(0.2)
        raise AcpError(f"ACP WebSocket not ready at {safe}: {last}")

    async def initialize(self) -> dict[str, Any]:
        result = await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {
                    "name": CLIENT_NAME,
                    "title": "RLE Grok Build harness",
                    "version": CLIENT_VERSION,
                },
            },
        )
        if not isinstance(result, dict):
            raise AcpError(f"initialize returned {result!r}")
        caps = result.get("agentCapabilities") or {}
        session_caps = caps.get("sessionCapabilities") or {}
        self._can_close_session = isinstance(session_caps, dict) and "close" in session_caps
        return result

    async def new_session(self, cwd: str) -> str:
        result = await self.request(
            "session/new",
            {
                "cwd": cwd,
                "mcpServers": [],
                "_meta": {"yoloMode": True},
            },
        )
        if not isinstance(result, dict) or not result.get("sessionId"):
            raise AcpError(f"session/new returned {result!r}")
        self._session_id = str(result["sessionId"])
        return self._session_id

    async def prompt(self, text: str) -> TurnResult:
        if not self._session_id:
            raise AcpError("ACP session is not open")
        acc = _TurnAccumulator()
        self._turn = acc
        try:
            result = await self.request(
                "session/prompt",
                {
                    "sessionId": self._session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
            )
        finally:
            self._turn = None
        stop: str | None = None
        if isinstance(result, dict):
            raw_stop = result.get("stopReason")
            if raw_stop is not None:
                stop = str(raw_stop)
            acc.absorb_provider_fields(result)
        return acc.to_turn_result(stop_reason=stop, session_id=self._session_id)

    async def cancel(self) -> None:
        if not self._session_id or self._ws is None:
            return
        await self.notify("session/cancel", {"sessionId": self._session_id})

    async def close(self) -> None:
        try:
            if self._session_id and self._ws is not None and self._can_close_session:
                try:
                    await asyncio.wait_for(
                        self.request("session/close", {"sessionId": self._session_id}),
                        timeout=3,
                    )
                except Exception:
                    logger.debug("ACP session/close failed", exc_info=True)
        finally:
            self._session_id = None
            self._fail_pending(AcpError("ACP client closed"))
            if self._reader is not None:
                self._reader.cancel()
                try:
                    await self._reader
                except (asyncio.CancelledError, Exception):
                    pass
                self._reader = None
            if self._ws is not None:
                try:
                    await self._ws.close()
                except Exception:
                    logger.debug("ACP WebSocket close failed", exc_info=True)
                self._ws = None

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        rid = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[rid] = fut
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            await self._send(payload)
            return await fut
        except asyncio.CancelledError:
            self._pending.pop(rid, None)
            raise

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._send(payload)

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            raise AcpError("ACP WebSocket is not connected")
        await self._ws.send(json.dumps(payload, ensure_ascii=False))

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if not raw:
                    continue
                text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    logger.debug("ACP non-JSON frame: %s", text[:200])
                    continue
                if isinstance(msg, dict):
                    await self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except ConnectionClosed:
            self._fail_pending(AcpError("ACP WebSocket closed"))
        except Exception as exc:
            self._fail_pending(AcpError(f"ACP reader failed: {exc}"))

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        mid = msg.get("id")
        if method == "session/update":
            params = msg.get("params") or {}
            if isinstance(params, dict) and self._turn is not None:
                self._turn.on_update(params)
            return
        if method == "session/request_permission" and mid is not None:
            await self._allow_permission(mid, msg.get("params") or {})
            return
        if method and mid is not None:
            await self._send({"jsonrpc": "2.0", "id": mid, "result": {}})
            return
        if mid is None:
            return
        try:
            rid = int(mid)
        except (TypeError, ValueError):
            return
        fut = self._pending.pop(rid, None)
        if fut is None or fut.done():
            return
        if "error" in msg:
            fut.set_exception(AcpError(f"ACP {msg['error']}"))
            return
        fut.set_result(msg.get("result"))

    async def _allow_permission(self, mid: Any, params: Any) -> None:
        options: Sequence[Any] = []
        if isinstance(params, dict):
            raw = params.get("options") or []
            if isinstance(raw, list):
                options = raw
        option_id: str | None = None
        for opt in options:
            if isinstance(opt, dict) and str(opt.get("kind", "")).startswith("allow"):
                raw_id = opt.get("optionId")
                if raw_id is not None:
                    option_id = str(raw_id)
                    break
        if option_id is None and options and isinstance(options[0], dict):
            raw_id = options[0].get("optionId")
            if raw_id is not None:
                option_id = str(raw_id)
        if option_id is None:
            result: dict[str, Any] = {"outcome": {"outcome": "cancelled"}}
        else:
            result = {"outcome": {"outcome": "selected", "optionId": option_id}}
        await self._send({"jsonrpc": "2.0", "id": mid, "result": result})

    def _fail_pending(self, exc: Exception) -> None:
        pending = list(self._pending.items())
        self._pending.clear()
        for _rid, fut in pending:
            if not fut.done():
                fut.set_exception(exc)
