"""Contract + invocation-shaping tests (no grok binary required)."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from rle.config import RLEConfig
from rle.harness import (
    HarnessContext,
    HarnessOptionsError,
    HarnessStepError,
    get_plugin,
    harness_names,
)
from rle.rimapi.client import RimAPIClient
from rle.testing import MockRimAPI, run_harness_smoke

from rle_harness_grok_build.argv_json import ARGV_JSON_ENV
from rle_harness_grok_build.harness import (
    GrokBuildHarness,
    binary_version,
    build_command,
    mcp_config_toml,
    parse_json_output,
)
from rle_harness_grok_build.isolated_home import DEFAULT_ADVERTISED_MCP_URL
from rle_harness_grok_build.options import GrokBuildOptions

NAME = "grok-build"


class TestRegistration:
    def test_entry_point(self) -> None:
        assert NAME in harness_names()
        assert get_plugin(NAME).option_schema() is GrokBuildOptions

    async def test_smoke_round_trip(self) -> None:
        report = await run_harness_smoke(NAME, ticks=2)
        assert report.ok and report.harness == NAME
        assert all(t.execution.executed == 1 for t in report.ticks)

    async def test_options_validated(self) -> None:
        with pytest.raises(HarnessOptionsError):
            await run_harness_smoke(NAME, ticks=1, options={"max_turns": 0})


class TestInvocationShaping:
    def test_mcp_config(self) -> None:
        toml = mcp_config_toml("http://127.0.0.1:7000/mcp")
        assert "[mcp_servers.rle]" in toml and 'url = "http://127.0.0.1:7000/mcp"' in toml
        assert "[compat.claude]" in toml and "mcps = false" in toml
        assert "[compat.cursor]" in toml
        assert "[model." not in toml
        assert "openrouter" not in toml

    def test_build_command_first_tick(self) -> None:
        cmd = build_command(
            "/bin/grok", "do it", GrokBuildOptions(max_turns=6, reasoning_effort="high"),
            model="grok-4.6", workdir="/tmp/w", session_id=None,
        )
        assert cmd[:3] == ["/bin/grok", "-p", "do it"]
        assert "--output-format" in cmd and cmd[cmd.index("--output-format") + 1] == "json"
        assert "--yolo" in cmd and "--no-subagents" in cmd and "--no-plan" in cmd
        assert cmd[cmd.index("-m") + 1] == "grok-4.6"
        assert "--resume" not in cmd
        assert cmd[cmd.index("--max-turns") + 1] == "6"
        assert cmd[cmd.index("--reasoning-effort") + 1] == "high"
        disallowed = cmd[cmd.index("--disallowed-tools") + 1]
        assert "run_terminal_cmd" in disallowed and "web_search" in disallowed

    def test_build_command_resumes(self) -> None:
        cmd = build_command(
            "grok", "again", GrokBuildOptions(), model=None, workdir="/tmp/w", session_id="abc",
        )
        assert cmd[cmd.index("--resume") + 1] == "abc"
        assert "-m" not in cmd
        cmd = build_command(
            "grok", "again", GrokBuildOptions(resume_session=False), model=None,
            workdir="/tmp/w", session_id="abc",
        )
        assert "--resume" not in cmd

    def test_parse_json_output(self) -> None:
        payload = {
            "text": "Placed walls.", "stopReason": "end_turn", "sessionId": "s-1",
            "num_turns": 4,
            "usage": {"input_tokens": 700, "cache_read_input_tokens": 4000,
                      "cache_creation_input_tokens": 0, "output_tokens": 180,
                      "reasoning_tokens": 40, "total_tokens": 4920},
            "total_cost_usd": 0.0127,
        }
        turn = parse_json_output("some log line\n" + json.dumps(payload))
        assert turn.text == "Placed walls."
        assert (turn.prompt_tokens, turn.completion_tokens) == (4700, 180)
        assert turn.reasoning_tokens == 40
        assert turn.extras["session_id"] == "s-1"
        assert turn.extras["cost_usd"] == 0.0127
        assert turn.extras["cost_source"] == "billed"
        assert turn.extras["num_turns"] == 4
        assert turn.extras["usage"]["input_tokens"] == 700

    def test_parse_json_output_cost_in_usd_alias(self) -> None:
        payload = {
            "text": "ok", "sessionId": "s-2",
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "cost_in_usd": "0.0042",
            "requestId": "req-alias-1",
        }
        turn = parse_json_output(json.dumps(payload))
        assert turn.prompt_tokens == 0
        assert turn.completion_tokens == 0
        assert turn.extras["cost_usd"] == 0.0042
        assert turn.extras["cost_source"] == "billed"
        assert turn.extras["generation_id"] == "req-alias-1"
        assert turn.extras["generation_ids"] == ["req-alias-1"]

    def test_parse_json_output_no_cost_leaves_source_unset(self) -> None:
        payload = {
            "text": "ok", "sessionId": "s-3",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }
        turn = parse_json_output(json.dumps(payload))
        assert "cost_usd" not in turn.extras
        assert "cost_source" not in turn.extras
        assert turn.extras["usage"]["input_tokens"] == 10

    def test_parse_error_object(self) -> None:
        with pytest.raises(HarnessStepError, match="Couldn't start"):
            parse_json_output('{"type":"error","message":"Couldn\'t start session: x"}')

    def test_parse_non_json(self) -> None:
        assert parse_json_output("plain text").text == "plain text"


def _fake_grok(tmp_path: Path) -> Path:
    """A stand-in `grok` that records argv and emits a headless json object."""
    script = tmp_path / "grok"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, os\n"
        f"open({str(tmp_path / 'argv.json')!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "assert os.path.exists(os.path.join(os.getcwd(), '.grok', 'config.toml'))\n"
        "if sys.argv[1:3] == ['mcp', 'list']:\n"
        "    print('rle')\n"
        "    sys.exit(0)\n"
        "print(json.dumps({'text': 'ok', 'sessionId': 'sess-9', "
        "'usage': {'input_tokens': 5, 'output_tokens': 1}}))\n",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _prompt_argvs(tmp_path: Path) -> list[list[str]]:
    """Argv lines from headless `-p` turns only (skip `mcp list` healthchecks)."""
    lines = (tmp_path / "argv.json").read_text().splitlines()
    return [json.loads(line) for line in lines if json.loads(line)[:1] == ["-p"]]


class TestAgainstFakeBinary:
    async def test_two_turns_resume_session(self, tmp_path: Path) -> None:
        fake = _fake_grok(tmp_path)
        harness = GrokBuildHarness(GrokBuildOptions(binary=str(fake), model="grok-4.6"))
        mock = MockRimAPI()
        async with RimAPIClient("http://mock") as client:
            mock.attach(client)
            ctx = HarnessContext(config=RLEConfig(tick_interval=0.0), client=client)
            harness._ctx = ctx
            await harness.start_agent("http://127.0.0.1:1/mcp")
            try:
                turn1 = await harness.send_turn("turn one")
                turn2 = await harness.send_turn("turn two")
            finally:
                await harness.stop_agent()
        assert turn1.text == "ok" and turn1.prompt_tokens == 5
        assert turn2.extras["session_id"] == "sess-9"
        argvs = _prompt_argvs(tmp_path)
        assert len(argvs) == 2
        assert "--resume" not in argvs[0]
        assert argvs[1][argvs[1].index("--resume") + 1] == "sess-9"
        assert argvs[0][argvs[0].index("-m") + 1] == "grok-4.6"

    async def test_advertise_url_written_to_isolated_home(self, tmp_path: Path) -> None:
        fake = _fake_grok(tmp_path)
        harness = GrokBuildHarness(GrokBuildOptions(
            binary=str(fake),
            mcp_advertise_url=DEFAULT_ADVERTISED_MCP_URL,
        ))
        mock = MockRimAPI()
        async with RimAPIClient("http://mock") as client:
            mock.attach(client)
            harness._ctx = HarnessContext(config=RLEConfig(tick_interval=0.0), client=client)
            await harness.start_agent("http://127.0.0.1:54321/mcp")
            try:
                assert harness._grok_home is not None and harness._workdir is not None
                home_cfg = (harness._grok_home / "config.toml").read_text(encoding="utf-8")
                proj_cfg = (Path(harness._workdir) / ".grok" / "config.toml").read_text(
                    encoding="utf-8",
                )
                assert DEFAULT_ADVERTISED_MCP_URL in home_cfg
                assert "127.0.0.1:54321" not in home_cfg
                assert DEFAULT_ADVERTISED_MCP_URL in proj_cfg
                assert "[model." not in home_cfg
                assert "openrouter" not in home_cfg
                env = harness._subprocess_env()
                assert env["MCP_URL"] == DEFAULT_ADVERTISED_MCP_URL
                assert "GROK_BASE_URL" not in env
                assert "OPENAI_COMPAT" not in env
            finally:
                await harness.stop_agent()

    async def test_openai_compat_writes_openrouter_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("XAI_API_KEY", raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-not-a-real-key")
        fake = _fake_grok(tmp_path)
        harness = GrokBuildHarness(GrokBuildOptions(
            binary=str(fake),
            model="google/gemini-3.8-flash",
            openai_compat=True,
            api_key_env="OPENROUTER_API_KEY",
        ))
        mock = MockRimAPI()
        async with RimAPIClient("http://mock") as client:
            mock.attach(client)
            harness._ctx = HarnessContext(config=RLEConfig(tick_interval=0.0), client=client)
            await harness.start_agent("http://127.0.0.1:1/mcp")
            try:
                assert harness._grok_home is not None and harness._workdir is not None
                home_cfg = (harness._grok_home / "config.toml").read_text(encoding="utf-8")
                proj_cfg = (Path(harness._workdir) / ".grok" / "config.toml").read_text(
                    encoding="utf-8",
                )
                for cfg in (home_cfg, proj_cfg):
                    assert "[mcp_servers.rle]" in cfg
                    assert "[compat.claude]" in cfg and "mcps = false" in cfg
                    assert '[model."google/gemini-3.8-flash"]' in cfg
                    assert 'base_url = "https://openrouter.ai/api/v1"' in cfg
                    assert 'env_key = "OPENROUTER_API_KEY"' in cfg
                    assert "sk-or-test-not-a-real-key" not in cfg
                env = harness._subprocess_env()
                assert env["GROK_MODEL"] == "google/gemini-3.8-flash"
                assert env["GROK_BASE_URL"] == "https://openrouter.ai/api/v1"
                assert env["GROK_API_KEY_ENV"] == "OPENROUTER_API_KEY"
                assert env["OPENAI_COMPAT"] == "true"
                assert "XAI_API_KEY" not in env
            finally:
                await harness.stop_agent()

    async def test_nonzero_exit_is_a_step_error(self, tmp_path: Path) -> None:
        # Healthcheck needs `mcp list` to succeed; only headless `-p` should fail.
        fake = tmp_path / "grok"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "if sys.argv[1:3] == ['mcp', 'list']:\n"
            "    print('rle')\n"
            "    raise SystemExit(0)\n"
            "print('auth failed', file=sys.stderr)\n"
            "raise SystemExit(1)\n",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        harness = GrokBuildHarness(GrokBuildOptions(binary=str(fake)))
        mock = MockRimAPI()
        async with RimAPIClient("http://mock") as client:
            mock.attach(client)
            harness._ctx = HarnessContext(config=RLEConfig(tick_interval=0.0), client=client)
            await harness.start_agent("http://127.0.0.1:1/mcp")
            try:
                with pytest.raises(HarnessStepError, match="auth failed"):
                    await harness.send_turn("x")
            finally:
                await harness.stop_agent()

    async def test_docker_wrapper_send_writes_argv_json(self, tmp_path: Path) -> None:
        fake = _fake_grok_docker_wrapper(tmp_path)
        huge = 'RLE turn — tick 0: colonist "Lee" needs a bed\n' + ("priority " * 200)
        harness = GrokBuildHarness(GrokBuildOptions(binary=str(fake), model="grok-4.6"))
        mock = MockRimAPI()
        async with RimAPIClient("http://mock") as client:
            mock.attach(client)
            harness._ctx = HarnessContext(config=RLEConfig(tick_interval=0.0), client=client)
            await harness.start_agent("http://127.0.0.1:1/mcp")
            try:
                turn = await harness.send_turn(huge)
            finally:
                await harness.stop_agent()
        assert turn.text == "ok"
        records = [
            json.loads(line) for line in (tmp_path / "wrapper.json").read_text().splitlines()
        ]
        assert len(records) == 2
        health, tick = records
        assert health["cli"] == []
        assert health["json"] == ["mcp", "list"]
        assert health["sidecar"]
        assert tick["cli"] == []
        assert tick["json"][0] == "-p"
        assert tick["json"][1] == huge
        assert "--output-format" in tick["json"]
        sidecar_path = Path(tick["sidecar"])
        assert not sidecar_path.exists()


def _fake_grok_docker_wrapper(tmp_path: Path) -> Path:
    """Wrapper-named stand-in that reads RLE_GROK_ARGV_JSON and records both argv sources."""
    script = tmp_path / "grok-docker.sh"
    record = tmp_path / "wrapper.json"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"record = {str(record)!r}\n"
        f"env_name = {ARGV_JSON_ENV!r}\n"
        "sidecar = os.environ.get(env_name)\n"
        "loaded = json.load(open(sidecar, encoding='utf-8')) if sidecar else None\n"
        "open(record, 'a').write(json.dumps({\n"
        "    'cli': sys.argv[1:], 'json': loaded, 'sidecar': sidecar,\n"
        "}) + '\\n')\n"
        "args = loaded if loaded is not None else sys.argv[1:]\n"
        "if args[:2] == ['mcp', 'list']:\n"
        "    print('rle')\n"
        "    raise SystemExit(0)\n"
        "print(json.dumps({'text': 'ok', 'sessionId': 'sess-9', "
        "'usage': {'input_tokens': 5, 'output_tokens': 1}}))\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


class TestStaleArgvJsonEnv:
    def test_subprocess_env_pops_stale_sidecar(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ARGV_JSON_ENV, "/tmp/does-not-exist-rle-grok-argv.json")
        harness = GrokBuildHarness(GrokBuildOptions())
        env = harness._subprocess_env()
        assert ARGV_JSON_ENV not in env
        assert ARGV_JSON_ENV in os.environ

    def test_binary_version_uses_fresh_sidecar_for_absolute_cmd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        wrapper = tmp_path / "grok-docker.cmd"
        wrapper.write_text("@echo off\r\n", encoding="utf-8")
        # Not +x: shutil.which misses Windows .cmd; resolve_binary still finds it.
        stale = tmp_path / "missing.json"
        monkeypatch.setenv(ARGV_JSON_ENV, str(stale))
        captured: dict[str, object] = {}

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            env = kwargs.get("env")
            assert isinstance(env, dict)
            sidecar = env.get(ARGV_JSON_ENV)
            assert isinstance(sidecar, str)
            captured["cmd"] = cmd
            captured["sidecar"] = sidecar
            captured["argv"] = json.loads(Path(sidecar).read_text(encoding="utf-8"))
            return subprocess.CompletedProcess(cmd, 0, stdout="grok 1.2.3\n", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert binary_version(str(wrapper)) == "grok 1.2.3"
        assert captured["cmd"] == [str(wrapper.resolve())]
        assert captured["argv"] == ["--version"]
        assert captured["sidecar"] != str(stale)
        assert not Path(str(captured["sidecar"])).exists()
