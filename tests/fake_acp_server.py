"""In-process ACP WebSocket server for unit tests (no live Grok).

Speaks the same JSON-RPC methods the harness uses: initialize, session/new,
session/prompt, session/cancel, session/close, session/request_permission.
Auth matches grok ``server.rs``: Bearer or ``?server-key=``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response


@dataclass
class FakeAcpState:
    methods: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    cancelled: bool = False
    session_id: str = "sess-acp-1"
    prompt_delay_s: float = 0.0
    text: str = "ok from acp"
    used_tokens: int = 12
    cost: dict[str, Any] | None = field(
        default_factory=lambda: {"amount": 0.01, "currency": "USD"},
    )
    generation_id: str | None = None
    request_permission: bool = False
    next_session: int = 1


def _auth_token(request: Request) -> str | None:
    header = request.headers.get("Authorization")
    if header and header.startswith("Bearer "):
        return header[7:]
    query = parse_qs(urlparse(request.path).query)
    keys = query.get("server-key") or []
    return keys[0] if keys else None


def make_process_request(secret: str):
    def process_request(connection: ServerConnection, request: Request) -> Response | None:
        path = urlparse(request.path).path
        if path != "/ws":
            return connection.respond(404, "Not Found")
        if _auth_token(request) != secret:
            return connection.respond(401, "Invalid or missing authorization token")
        return None

    return process_request


async def handle_connection(ws: ServerConnection, state: FakeAcpState) -> None:
    send_lock = asyncio.Lock()

    async def send(payload: dict[str, Any]) -> None:
        async with send_lock:
            await ws.send(json.dumps(payload))

    async for raw in ws:
        text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        method = msg.get("method")
        if method == "session/prompt":
            asyncio.create_task(_handle_message(ws, state, msg, send))
            continue
        await _handle_message(ws, state, msg, send)


async def _handle_message(
    _ws: ServerConnection,
    state: FakeAcpState,
    msg: dict[str, Any],
    send: Any,
) -> None:
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    if isinstance(method, str):
        state.methods.append(method)
    if method == "initialize":
        await send({
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": 1,
                "agentCapabilities": {
                    "loadSession": True,
                    "mcpCapabilities": {"http": True},
                    "sessionCapabilities": {"close": {}},
                },
                "agentInfo": {"name": "fake-grok", "version": "test"},
            },
        })
        return
    if method == "session/new":
        sid = state.session_id
        if state.next_session > 1:
            sid = f"{state.session_id}-{state.next_session}"
        state.next_session += 1
        await send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": sid}})
        return
    if method == "session/prompt":
        prompt_text = ""
        for block in params.get("prompt") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                prompt_text += str(block.get("text", ""))
        state.prompts.append(prompt_text)
        if state.request_permission:
            state.methods.append("session/request_permission")
            await send({
                "jsonrpc": "2.0",
                "id": "perm-1",
                "method": "session/request_permission",
                "params": {
                    "sessionId": state.session_id,
                    "toolCall": {"toolCallId": "call_1"},
                    "options": [
                        {"optionId": "allow-once", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
                    ],
                },
            })
        await send({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": state.session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": state.text},
                },
            },
        })
        usage_update: dict[str, Any] = {
            "sessionUpdate": "usage_update",
            "used": state.used_tokens,
            "size": 100000,
        }
        if state.cost is not None:
            usage_update["cost"] = state.cost
        if state.generation_id:
            usage_update["generation_id"] = state.generation_id
        await send({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": state.session_id,
                "update": usage_update,
            },
        })
        if state.prompt_delay_s:
            slept = 0.0
            while slept < state.prompt_delay_s and not state.cancelled:
                await asyncio.sleep(0.05)
                slept += 0.05
        reason = "cancelled" if state.cancelled else "end_turn"
        await send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": reason}})
        return
    if method == "session/cancel":
        state.cancelled = True
        return
    if method == "session/close":
        await send({"jsonrpc": "2.0", "id": mid, "result": {}})
        return
    if mid is not None:
        await send({"jsonrpc": "2.0", "id": mid, "result": {}})


async def run_fake_acp(
    host: str,
    port: int,
    secret: str,
    state: FakeAcpState | None = None,
    ready: asyncio.Event | None = None,
) -> None:
    agent = state or FakeAcpState()

    async def handler(ws: ServerConnection) -> None:
        await handle_connection(ws, agent)

    async with serve(
        handler, host, port, process_request=make_process_request(secret),
    ):
        if ready is not None:
            ready.set()
        await asyncio.Future()


def serve_forever(bind: str, secret: str, *, delay_s: float = 0.0) -> None:
    host, _, port_s = bind.rpartition(":")
    host = host.strip().strip("[]") or "127.0.0.1"
    state = FakeAcpState(prompt_delay_s=delay_s)
    asyncio.run(run_fake_acp(host, int(port_s), secret, state))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fake Grok ACP WebSocket server")
    parser.add_argument("--bind", required=True, help="host:port")
    parser.add_argument("--secret", required=True)
    parser.add_argument("--delay", type=float, default=0.0)
    args = parser.parse_args(argv)
    serve_forever(args.bind, args.secret, delay_s=args.delay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
