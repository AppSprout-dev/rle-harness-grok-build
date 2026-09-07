"""Warm persist helpers + harness lifecycle (mock wrapper; no live Grok/Docker)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from rle.config import RLEConfig
from rle.harness import HarnessContext, HarnessStepError
from rle.rimapi.client import RimAPIClient
from rle.testing import MockRimAPI

from rle_harness_grok_build.argv_json import ARGV_JSON_ENV
from rle_harness_grok_build.harness import GrokBuildHarness
from rle_harness_grok_build.options import GrokBuildOptions
from rle_harness_grok_build.persist import (
    ACP_SERVE_ARG,
    PERSIST_ACTION_ENV,
    PERSIST_CONTAINER_ENV,
    PERSIST_KEEPALIVE_ARG,
    apply_acp_env,
    apply_persist_env,
    new_container_name,
    persist_start_args,
)


class TestPersistHelpers:
    def test_container_name_shape(self) -> None:
        name = new_container_name()
        assert name.startswith("rle-grok-")
        assert len(name) == len("rle-grok-") + 12
        assert name.replace("-", "").isalnum()
        assert new_container_name() != name

    def test_persist_start_args(self) -> None:
        assert persist_start_args("/bin/grok-docker.sh", "/tmp/w") == [
            "/bin/grok-docker.sh", "--cwd", "/tmp/w", PERSIST_KEEPALIVE_ARG,
        ]
        assert persist_start_args(
            "/bin/grok-docker.sh", "/tmp/w", acp=True, agent_flags=["-m", "grok-4.6"],
        ) == [
            "/bin/grok-docker.sh", "--cwd", "/tmp/w", "-m", "grok-4.6", ACP_SERVE_ARG,
        ]
        assert persist_start_args(
            "/bin/grok-docker.sh",
            "/tmp/w",
            acp=True,
            agent_flags=[
                "--cwd", "/evil",
                "--max-turns", "9",
                "--disallowed-tools", "x",
                "--yolo",
                "--no-subagents",
                "-m",
                "grok-4.6",
                "--no-plan",
                "--output-format", "json",
                "--resume", "sid",
            ],
        ) == [
            "/bin/grok-docker.sh", "--cwd", "/tmp/w", "-m", "grok-4.6", ACP_SERVE_ARG,
        ]

    def test_apply_acp_env(self) -> None:
        env: dict[str, str] = {}
        apply_acp_env(env, publish="127.0.0.1:9:2419", secret="tok")
        assert env["RLE_GROK_ACP_PUBLISH"] == "127.0.0.1:9:2419"
        assert env["GROK_AGENT_SECRET"] == "tok"

    def test_apply_persist_env(self) -> None:
        env: dict[str, str] = {}
        apply_persist_env(env, container="rle-grok-abc", action="exec")
        assert env[PERSIST_CONTAINER_ENV] == "rle-grok-abc"
        assert env[PERSIST_ACTION_ENV] == "exec"

    def test_apply_persist_env_rejects_bad_action(self) -> None:
        with pytest.raises(ValueError, match="unknown persist action"):
            apply_persist_env({}, container="x", action="nope")  # type: ignore[arg-type]

    def test_warm_or_persistent_enables(self) -> None:
        assert not GrokBuildOptions().warm_enabled
        assert GrokBuildOptions(warm=True).warm_enabled
        assert GrokBuildOptions(persistent=True).warm_enabled
        assert GrokBuildOptions(warm=True, persistent=True).warm_enabled
        assert not GrokBuildOptions().acp_enabled
        assert GrokBuildOptions(acp=True).acp_enabled
        assert GrokBuildOptions(mode="acp").acp_enabled


def _fake_persist_wrapper(tmp_path: Path) -> Path:
    """grok-docker.sh stand-in that records persist action + argv JSON."""
    script = tmp_path / "grok-docker.sh"
    record = tmp_path / "persist.json"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"record = {str(record)!r}\n"
        f"env_name = {ARGV_JSON_ENV!r}\n"
        f"action_env = {PERSIST_ACTION_ENV!r}\n"
        f"name_env = {PERSIST_CONTAINER_ENV!r}\n"
        "sidecar = os.environ.get(env_name)\n"
        "loaded = json.load(open(sidecar, encoding='utf-8')) if sidecar else None\n"
        "action = os.environ.get(action_env)\n"
        "open(record, 'a').write(json.dumps({\n"
        "    'action': action,\n"
        "    'container': os.environ.get(name_env),\n"
        "    'cli': sys.argv[1:],\n"
        "    'json': loaded,\n"
        "}) + '\\n')\n"
        "args = loaded if loaded is not None else sys.argv[1:]\n"
        "if action == 'start':\n"
        "    print('cid-persist')\n"
        "    raise SystemExit(0)\n"
        "if action == 'stop':\n"
        "    raise SystemExit(0)\n"
        "if args[:2] == ['mcp', 'list']:\n"
        "    print('rle')\n"
        "    raise SystemExit(0)\n"
        "print(json.dumps({'text': 'ok', 'sessionId': 'sess-warm',\n"
        "                  'usage': {'input_tokens': 5, 'output_tokens': 1}}))\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _records(tmp_path: Path) -> list[dict[object, object]]:
    return [json.loads(line) for line in (tmp_path / "persist.json").read_text().splitlines()]


async def _run_two_turns(harness: GrokBuildHarness) -> None:
    mock = MockRimAPI()
    async with RimAPIClient("http://mock") as client:
        mock.attach(client)
        harness._ctx = HarnessContext(config=RLEConfig(tick_interval=0.0), client=client)
        await harness.start_agent("http://127.0.0.1:1/mcp")
        try:
            await harness.send_turn("turn one")
            await harness.send_turn("turn two")
        finally:
            await harness.stop_agent()


class TestWarmHarnessLifecycle:
    async def test_wrapper_start_exec_stop_and_resume(self, tmp_path: Path) -> None:
        fake = _fake_persist_wrapper(tmp_path)
        harness = GrokBuildHarness(GrokBuildOptions(
            binary=str(fake), model="grok-4.6", warm=True,
        ))
        await _run_two_turns(harness)
        rows = _records(tmp_path)
        actions = [row["action"] for row in rows]
        assert actions[0] == "start"
        assert "exec" in actions[1:]
        assert actions[-1] == "stop"
        assert actions.count("start") == 1
        assert actions.count("stop") == 1
        names = {row["container"] for row in rows}
        assert len(names) == 1
        name = next(iter(names))
        assert isinstance(name, str) and name.startswith("rle-grok-")
        start = rows[0]
        assert start["cli"] == []
        assert start["json"][0] == "--cwd"
        assert start["json"][-1] == "persist"
        ticks = [row for row in rows if row["json"] and row["json"][0] == "-p"]
        assert len(ticks) == 2
        assert "--resume" not in ticks[0]["json"]
        assert ticks[1]["json"][ticks[1]["json"].index("--resume") + 1] == "sess-warm"
        assert harness._persist_container is None

    async def test_persistent_alias(self, tmp_path: Path) -> None:
        fake = _fake_persist_wrapper(tmp_path)
        harness = GrokBuildHarness(GrokBuildOptions(binary=str(fake), persistent=True))
        await _run_two_turns(harness)
        assert _records(tmp_path)[0]["action"] == "start"

    async def test_host_grok_warm_skips_persist_env(self, tmp_path: Path) -> None:
        script = tmp_path / "grok"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            f"open({str(tmp_path / 'env.json')!r}, 'a').write(json.dumps({{\n"
            f"    'action': os.environ.get({PERSIST_ACTION_ENV!r}),\n"
            "}) + '\\n')\n"
            "if sys.argv[1:3] == ['mcp', 'list']:\n"
            "    print('rle')\n"
            "    raise SystemExit(0)\n"
            "print(json.dumps({'text': 'ok', 'sessionId': 's',\n"
            "                  'usage': {'input_tokens': 1, 'output_tokens': 1}}))\n",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        harness = GrokBuildHarness(GrokBuildOptions(binary=str(script), warm=True))
        await _run_two_turns(harness)
        rows = [json.loads(line) for line in (tmp_path / "env.json").read_text().splitlines()]
        assert all(row["action"] is None for row in rows)
        assert harness._persist_container is None

    async def test_cold_wrapper_has_no_persist_action(self, tmp_path: Path) -> None:
        fake = _fake_persist_wrapper(tmp_path)
        harness = GrokBuildHarness(GrokBuildOptions(binary=str(fake), warm=False))
        await _run_two_turns(harness)
        rows = _records(tmp_path)
        assert all(row["action"] is None for row in rows)
        assert all(row["container"] is None for row in rows)

    async def test_persist_start_failure(self, tmp_path: Path) -> None:
        script = tmp_path / "grok-docker.sh"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            f"if os.environ.get({PERSIST_ACTION_ENV!r}) == 'start':\n"
            "    print('cannot start', file=sys.stderr)\n"
            "    raise SystemExit(2)\n"
            "print('rle')\n",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        harness = GrokBuildHarness(GrokBuildOptions(binary=str(script), warm=True))
        mock = MockRimAPI()
        async with RimAPIClient("http://mock") as client:
            mock.attach(client)
            harness._ctx = HarnessContext(config=RLEConfig(tick_interval=0.0), client=client)
            with pytest.raises(HarnessStepError, match="persist start failed"):
                await harness.start_agent("http://127.0.0.1:1/mcp")
            await harness.stop_agent()
