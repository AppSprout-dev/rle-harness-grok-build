"""ACP client + harness path (mocked WebSocket server; no live Grok)."""

from __future__ import annotations

import asyncio
import json
import stat
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fake_acp_server import FakeAcpState, run_fake_acp
from rle.config import RLEConfig
from rle.harness import HarnessContext
from rle.rimapi.client import RimAPIClient
from rle.testing import MockRimAPI

from rle_harness_grok_build.acp import (
    ACP_CONTAINER_PORT,
    AcpClient,
    AcpError,
    acp_ws_url,
    agent_option_flags,
    build_agent_serve_command,
    client_ws_host,
    pick_loopback_port,
    redact_ws_url,
    resolve_acp_listen,
)
from rle_harness_grok_build.argv_json import ARGV_JSON_ENV
from rle_harness_grok_build.harness import GrokBuildHarness
from rle_harness_grok_build.options import GrokBuildOptions
from rle_harness_grok_build.persist import (
    ACP_PUBLISH_ENV,
    ACP_SECRET_ENV,
    ACP_SERVE_ARG,
    PERSIST_ACTION_ENV,
    PERSIST_CONTAINER_ENV,
    apply_acp_env,
    persist_start_args,
)

FAKE_SERVER = Path(__file__).resolve().parent / "fake_acp_server.py"


class TestAcpHelpers:
    def test_options_acp_and_mode(self) -> None:
        assert not GrokBuildOptions().acp_enabled
        assert GrokBuildOptions(acp=True).acp_enabled
        assert GrokBuildOptions(mode="acp").acp_enabled
        assert GrokBuildOptions(mode="ACP").acp_enabled
        assert not GrokBuildOptions(mode="prompt").acp_enabled
        assert GrokBuildOptions(acp=True, mode="prompt").acp_enabled

    def test_resolve_bind_and_client_host(self) -> None:
        host, port = resolve_acp_listen("127.0.0.1:2419")
        assert (host, port) == ("127.0.0.1", 2419)
        host, port = resolve_acp_listen(None)
        assert host == "127.0.0.1" and port > 0
        host, port = resolve_acp_listen("127.0.0.1:0")
        assert host == "127.0.0.1" and port > 0
        assert client_ws_host("0.0.0.0") == "127.0.0.1"
        assert client_ws_host("127.0.0.1") == "127.0.0.1"

    def test_redact_ws_url(self) -> None:
        url = acp_ws_url("127.0.0.1", 2419, secret="super-secret")
        assert "super-secret" in url
        assert "super-secret" not in redact_ws_url(url)
        assert "/ws" in url

    def test_build_agent_serve_command(self) -> None:
        cmd = build_agent_serve_command(
            "/bin/grok",
            bind="127.0.0.1:2419",
            secret="tok",
            cwd="/tmp/w",
            model="grok-4.6",
            opts=GrokBuildOptions(max_turns=20),
        )
        assert cmd[:3] == ["/bin/grok", "agent", "--always-approve"]
        assert cmd[cmd.index("--cwd") + 1] == "/tmp/w"
        assert cmd[cmd.index("-m") + 1] == "grok-4.6"
        assert cmd[cmd.index("--max-turns") + 1] == "20"
        assert "--disallowed-tools" in cmd
        assert "--no-subagents" not in cmd and "--no-plan" not in cmd
        serve_at = cmd.index("serve")
        assert cmd[serve_at:] == ["serve", "--bind", "127.0.0.1:2419", "--secret", "tok"]
        assert serve_at > cmd.index("--always-approve")

    def test_agent_option_flags_and_persist_start(self) -> None:
        flags = agent_option_flags(
            GrokBuildOptions(max_turns=6, extra_args=["--foo"]), model="grok-4.6",
        )
        assert flags[:2] == ["-m", "grok-4.6"]
        assert "--foo" in flags
        assert persist_start_args("/bin/grok-docker.sh", "/tmp/w", acp=True, agent_flags=flags) == [
            "/bin/grok-docker.sh", "--cwd", "/tmp/w", *flags, ACP_SERVE_ARG,
        ]

    def test_apply_acp_env(self) -> None:
        env: dict[str, str] = {}
        apply_acp_env(env, publish="127.0.0.1:9:2419", secret="tok")
        assert env[ACP_PUBLISH_ENV] == "127.0.0.1:9:2419"
        assert env[ACP_SECRET_ENV] == "tok"
        with pytest.raises(ValueError, match="secret"):
            apply_acp_env({}, publish="x", secret="")


@pytest.fixture
async def fake_acp() -> AsyncIterator[tuple[FakeAcpState, AcpClient]]:
    port = pick_loopback_port()
    secret = "test-secret"
    state = FakeAcpState()
    ready = asyncio.Event()
    task = asyncio.create_task(run_fake_acp("127.0.0.1", port, secret, state, ready))
    await asyncio.wait_for(ready.wait(), timeout=5)
    client = AcpClient(acp_ws_url("127.0.0.1", port, secret=secret), secret)
    try:
        await client.connect(timeout_s=5)
        yield state, client
    finally:
        await client.close()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class TestAcpClientProtocol:
    async def test_initialize_session_prompt_and_usage(
        self, fake_acp: tuple[FakeAcpState, AcpClient],
    ) -> None:
        state, client = fake_acp
        init = await client.initialize()
        assert init["protocolVersion"] == 1
        sid = await client.new_session("/tmp/work")
        assert sid == "sess-acp-1"
        turn = await client.prompt("RLE turn — tick 0")
        assert turn.text == "ok from acp"
        assert turn.prompt_tokens == 12
        assert turn.extras["stop_reason"] == "end_turn"
        assert turn.extras["session_id"] == "sess-acp-1"
        assert turn.extras["acp"] is True
        assert turn.extras["cost_usd"] == 0.01
        assert state.methods[:3] == ["initialize", "session/new", "session/prompt"]
        assert state.prompts == ["RLE turn — tick 0"]
        turn2 = await client.prompt("tick 1")
        assert turn2.extras["stop_reason"] == "end_turn"
        assert state.prompts == ["RLE turn — tick 0", "tick 1"]

    async def test_cancel_sets_cancelled_stop(
        self, fake_acp: tuple[FakeAcpState, AcpClient],
    ) -> None:
        state, client = fake_acp
        state.prompt_delay_s = 0.4
        await client.initialize()
        await client.new_session("/tmp/w")
        prompt_task = asyncio.create_task(client.prompt("slow"))
        await asyncio.sleep(0.05)
        await client.cancel()
        turn = await prompt_task
        assert state.cancelled is True
        assert "session/cancel" in state.methods
        assert turn.extras["stop_reason"] == "cancelled"

    async def test_permission_auto_allow(self) -> None:
        port = pick_loopback_port()
        secret = "perm-secret"
        state = FakeAcpState(request_permission=True)
        ready = asyncio.Event()
        task = asyncio.create_task(run_fake_acp("127.0.0.1", port, secret, state, ready))
        await asyncio.wait_for(ready.wait(), timeout=5)
        client = AcpClient(acp_ws_url("127.0.0.1", port, secret=secret), secret)
        try:
            await client.connect(timeout_s=5)
            await client.initialize()
            await client.new_session("/tmp/w")
            turn = await client.prompt("need tool")
            assert turn.extras["stop_reason"] == "end_turn"
            assert "session/request_permission" in state.methods
        finally:
            await client.close()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_rejects_bad_secret(self) -> None:
        port = pick_loopback_port()
        ready = asyncio.Event()
        task = asyncio.create_task(
            run_fake_acp("127.0.0.1", port, "real-secret", FakeAcpState(), ready),
        )
        await asyncio.wait_for(ready.wait(), timeout=5)
        client = AcpClient(acp_ws_url("127.0.0.1", port, secret="wrong"), "wrong")
        try:
            with pytest.raises(AcpError, match="not ready"):
                await client.connect(timeout_s=1)
        finally:
            await client.close()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def _fake_host_grok(tmp_path: Path) -> Path:
    script = tmp_path / "grok"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"open({str(tmp_path / 'argv.json')!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:3] == ['mcp', 'list']:\n"
        "    print('rle')\n"
        "    raise SystemExit(0)\n"
        "if 'serve' in sys.argv:\n"
        "    bind = sys.argv[sys.argv.index('--bind') + 1]\n"
        "    secret = sys.argv[sys.argv.index('--secret') + 1]\n"
        f"    cmd = [{sys.executable!r}, {str(FAKE_SERVER)!r},\n"
        "           '--bind', bind, '--secret', secret]\n"
        "    delay = os.environ.get('RLE_FAKE_ACP_DELAY')\n"
        "    if delay:\n"
        "        cmd += ['--delay', delay]\n"
        "    os.execv(cmd[0], cmd)\n"
        "raise SystemExit('unexpected argv: ' + ' '.join(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _fake_acp_docker_wrapper(tmp_path: Path) -> Path:
    script = tmp_path / "grok-docker.sh"
    record = tmp_path / "persist.json"
    pidfile = tmp_path / "acp.pid"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, signal, subprocess, sys\n"
        f"record = {str(record)!r}\n"
        f"pidfile = {str(pidfile)!r}\n"
        f"server = {str(FAKE_SERVER)!r}\n"
        f"py = {sys.executable!r}\n"
        f"env_name = {ARGV_JSON_ENV!r}\n"
        f"action_env = {PERSIST_ACTION_ENV!r}\n"
        f"name_env = {PERSIST_CONTAINER_ENV!r}\n"
        f"pub_env = {ACP_PUBLISH_ENV!r}\n"
        f"secret_env = {ACP_SECRET_ENV!r}\n"
        "sidecar = os.environ.get(env_name)\n"
        "loaded = json.load(open(sidecar, encoding='utf-8')) if sidecar else None\n"
        "action = os.environ.get(action_env)\n"
        "open(record, 'a').write(json.dumps({\n"
        "    'action': action,\n"
        "    'container': os.environ.get(name_env),\n"
        "    'publish': os.environ.get(pub_env),\n"
        "    'secret': os.environ.get(secret_env),\n"
        "    'cli': sys.argv[1:],\n"
        "    'json': loaded,\n"
        "}) + '\\n')\n"
        "args = loaded if loaded is not None else sys.argv[1:]\n"
        "if action == 'start':\n"
        "    pub = os.environ[pub_env]\n"
        "    host, port, _cport = pub.split(':')\n"
        "    proc = subprocess.Popen(\n"
        "        [py, server, '--bind', f'{host}:{port}',\n"
        "         '--secret', os.environ[secret_env]],\n"
        "        start_new_session=True,\n"
        "        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
        "    )\n"
        "    open(pidfile, 'w').write(str(proc.pid))\n"
        "    import socket, time\n"
        "    for _ in range(50):\n"
        "        if proc.poll() is not None:\n"
        "            raise SystemExit('fake acp server exited')\n"
        "        try:\n"
        "            s = socket.create_connection((host, int(port)), 0.1)\n"
        "            s.close()\n"
        "            break\n"
        "        except OSError:\n"
        "            time.sleep(0.05)\n"
        "    print('cid-acp')\n"
        "    raise SystemExit(0)\n"
        "if action == 'stop':\n"
        "    if os.path.exists(pidfile):\n"
        "        try:\n"
        "            os.kill(int(open(pidfile).read()), signal.SIGTERM)\n"
        "        except OSError:\n"
        "            pass\n"
        "    raise SystemExit(0)\n"
        "if args[:2] == ['mcp', 'list']:\n"
        "    print('rle')\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit('unexpected exec: ' + repr(args))\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


async def _two_acp_turns(harness: GrokBuildHarness) -> None:
    mock = MockRimAPI()
    async with RimAPIClient("http://mock") as client:
        mock.attach(client)
        harness._ctx = HarnessContext(config=RLEConfig(tick_interval=0.0), client=client)
        await harness.start_agent("http://127.0.0.1:1/mcp")
        try:
            t1 = await harness.send_turn("turn one")
            t2 = await harness.send_turn("turn two")
            assert t1.text == "ok from acp"
            assert t1.extras["acp"] is True
            assert t2.extras["stop_reason"] == "end_turn"
            assert harness._session_id
        finally:
            await harness.stop_agent()


class TestAcpHarnessLifecycle:
    async def test_host_grok_serve_two_ticks(self, tmp_path: Path) -> None:
        fake = _fake_host_grok(tmp_path)
        harness = GrokBuildHarness(GrokBuildOptions(
            binary=str(fake), model="grok-4.6", acp=True, acp_secret="host-secret",
        ))
        await _two_acp_turns(harness)
        lines = [json.loads(line) for line in (tmp_path / "argv.json").read_text().splitlines()]
        assert lines[0] == ["mcp", "list"]
        serve = next(row for row in lines if "serve" in row)
        assert serve[:2] == ["agent", "--always-approve"]
        assert "--cwd" in serve
        assert serve[serve.index("-m") + 1] == "grok-4.6"
        assert serve[serve.index("--secret") + 1] == "host-secret"
        assert "--bind" in serve
        assert harness._acp is None
        assert harness._serve_proc is None

    async def test_mode_acp_alias(self, tmp_path: Path) -> None:
        fake = _fake_host_grok(tmp_path)
        harness = GrokBuildHarness(GrokBuildOptions(binary=str(fake), mode="acp"))
        await _two_acp_turns(harness)

    async def test_docker_wrapper_starts_acp_serve_not_minus_p(
        self, tmp_path: Path,
    ) -> None:
        fake = _fake_acp_docker_wrapper(tmp_path)
        harness = GrokBuildHarness(GrokBuildOptions(
            binary=str(fake), acp=True, model="grok-4.6", acp_secret="dock-secret",
        ))
        await _two_acp_turns(harness)
        rows = [
            json.loads(line) for line in (tmp_path / "persist.json").read_text().splitlines()
        ]
        actions = [row["action"] for row in rows]
        assert actions[0] == "start"
        assert "exec" in actions
        assert actions[-1] == "stop"
        start = rows[0]
        assert start["publish"]
        assert start["secret"] == "dock-secret"
        host, port, cport = str(start["publish"]).split(":")
        assert host == "127.0.0.1"
        assert cport == str(ACP_CONTAINER_PORT)
        assert int(port) > 0
        assert start["json"][0] == "--cwd"
        assert start["json"][-1] == ACP_SERVE_ARG
        assert "-m" in start["json"]
        assert any(row["json"] == ["mcp", "list"] for row in rows)
        assert not any(row["json"] and row["json"][0] == "-p" for row in rows)
        assert harness._persist_container is None

    async def test_abort_sends_session_cancel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("RLE_FAKE_ACP_DELAY", "30")
        fake = _fake_host_grok(tmp_path)
        harness = GrokBuildHarness(GrokBuildOptions(
            binary=str(fake), acp=True, acp_secret="abort-secret",
        ))
        mock = MockRimAPI()
        async with RimAPIClient("http://mock") as client:
            mock.attach(client)
            harness._ctx = HarnessContext(config=RLEConfig(tick_interval=0.0), client=client)
            await harness.start_agent("http://127.0.0.1:1/mcp")
            try:
                send = asyncio.create_task(harness.send_turn("slow tick"))
                await asyncio.sleep(0.1)
                await harness.abort_turn()
                turn = await asyncio.wait_for(send, timeout=5)
                assert turn.extras["stop_reason"] == "cancelled"
            finally:
                await harness.stop_agent()
