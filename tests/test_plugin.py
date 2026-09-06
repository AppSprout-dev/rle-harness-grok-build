"""Contract + invocation-shaping tests (no grok binary required)."""

from __future__ import annotations

import json
import os
import stat
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

from rle_harness_grok_build.harness import (
    GrokBuildHarness,
    build_command,
    mcp_config_toml,
    parse_json_output,
)
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
        assert turn.extras["num_turns"] == 4

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
        argvs = [json.loads(line) for line in (tmp_path / "argv.json").read_text().splitlines()]
        assert "--resume" not in argvs[0]
        assert argvs[1][argvs[1].index("--resume") + 1] == "sess-9"
        assert argvs[0][argvs[0].index("-m") + 1] == "grok-4.6"
        assert os.environ  # sanity: env passthrough not modified

    async def test_nonzero_exit_is_a_step_error(self, tmp_path: Path) -> None:
        fake = tmp_path / "grok"
        fake.write_text("#!/bin/sh\necho 'auth failed' >&2\nexit 1\n")
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
