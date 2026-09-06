"""Unit tests for isolated GROK_HOME / Docker config helpers (no grok, no RimWorld)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rle_harness_grok_build.docker_cli import DockerCommand, build_parser, find_scenario
from rle_harness_grok_build.harness import mcp_config_toml as harness_mcp_config_toml
from rle_harness_grok_build.isolated_home import (
    DEFAULT_COMPOSE_RIMWORLD_URL,
    DEFAULT_DOCKER_RIMAPI_URL,
    DEFAULT_SMOKE_MCP_URL,
    FORBIDDEN_HOST_BIND_SOURCES,
    check_mcp_list_output,
    mcp_config_toml,
    prepare_runtime_grok_home,
    resolve_auth_json,
    resolve_mcp_url,
    resolve_rimapi_url,
    wipe_plugin_pollution,
    write_isolated_grok_home,
    write_project_grok_config,
)


class TestMcpConfig:
    def test_rle_only_and_compat_off(self) -> None:
        toml = mcp_config_toml("http://127.0.0.1:7000/mcp")
        assert "[mcp_servers.rle]" in toml
        assert 'url = "http://127.0.0.1:7000/mcp"' in toml
        assert "[compat.claude]" in toml and "mcps = false" in toml
        assert "[compat.cursor]" in toml
        assert harness_mcp_config_toml("http://x") == mcp_config_toml("http://x")

    def test_does_not_mention_host_plugin_servers(self) -> None:
        toml = mcp_config_toml("http://rle/mcp")
        assert "wandb" not in toml
        assert "huggingface" not in toml


class TestUrlResolution:
    def test_rimapi_flag_wins(self) -> None:
        assert resolve_rimapi_url("http://rimworld:8765/", env={"RIMAPI_URL": "http://other"}) == (
            "http://rimworld:8765"
        )

    def test_rimapi_env_then_default(self) -> None:
        assert resolve_rimapi_url(None, env={"RIMAPI_URL": "http://host:8765/"}) == "http://host:8765"
        assert resolve_rimapi_url(None, env={}) == DEFAULT_DOCKER_RIMAPI_URL

    def test_mcp_url(self) -> None:
        assert resolve_mcp_url("http://127.0.0.1:1/mcp", env={"MCP_URL": "x"}) == (
            "http://127.0.0.1:1/mcp"
        )
        assert resolve_mcp_url(None, env={"MCP_URL": "http://in/mcp"}) == "http://in/mcp"
        assert resolve_mcp_url(None, env={}) == DEFAULT_SMOKE_MCP_URL

    def test_compose_hostname_constant(self) -> None:
        assert DEFAULT_COMPOSE_RIMWORLD_URL == "http://rimworld:8765"


class TestAuthAndForbiddenMounts:
    def test_auth_flag_and_env(self, tmp_path: Path) -> None:
        flag = tmp_path / "flag.json"
        flag.write_text("{}", encoding="utf-8")
        assert resolve_auth_json(flag, env={"GROK_AUTH_JSON": "/nope"}) == flag
        assert resolve_auth_json(None, env={"GROK_AUTH_JSON": str(flag)}) == flag

    def test_auth_absent_without_mount(self) -> None:
        assert resolve_auth_json(None, env={}) is None

    def test_forbidden_host_sources_documented(self) -> None:
        assert "~/.grok/config.toml" in FORBIDDEN_HOST_BIND_SOURCES
        assert "~/.claude.json" in FORBIDDEN_HOST_BIND_SOURCES
        assert "~/.cursor" in FORBIDDEN_HOST_BIND_SOURCES


class TestWriteIsolatedHome:
    def test_writes_config_and_optional_auth_only(self, tmp_path: Path) -> None:
        home = tmp_path / "grok-home"
        auth = tmp_path / "auth.json"
        auth.write_text('{"token":"x"}', encoding="utf-8")
        host_config = tmp_path / "host-config.toml"
        host_config.write_text("[mcp_servers.wandb]\nurl = \"http://wandb\"\n", encoding="utf-8")
        write_isolated_grok_home(home, "http://127.0.0.1:9/mcp", auth_json=auth)
        written = (home / "config.toml").read_text(encoding="utf-8")
        assert written == mcp_config_toml("http://127.0.0.1:9/mcp")
        assert (home / "auth.json").read_text(encoding="utf-8") == '{"token":"x"}'
        assert not (home / "plugins").exists()
        # Host config.toml is never consulted.
        assert "wandb" not in written

    def test_skips_empty_auth(self, tmp_path: Path) -> None:
        home = tmp_path / "h"
        empty = tmp_path / "empty.json"
        empty.write_text("", encoding="utf-8")
        write_isolated_grok_home(home, "http://x/mcp", auth_json=empty)
        assert not (home / "auth.json").exists()

    def test_wipes_plugin_pollution(self, tmp_path: Path) -> None:
        home = tmp_path / "dirty"
        (home / "plugins" / "wandb").mkdir(parents=True)
        (home / "skills").mkdir()
        (home / "mcp.json").write_text("{}", encoding="utf-8")
        removed = wipe_plugin_pollution(home)
        write_isolated_grok_home(home, "http://rle/mcp")
        assert "plugins" in removed and "skills" in removed and "mcp.json" in removed
        assert not (home / "plugins").exists()
        assert "[compat.claude]" in (home / "config.toml").read_text(encoding="utf-8")

    def test_prepare_exports_grok_home_and_project_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home = tmp_path / "iso"
        work = tmp_path / "work"
        monkeypatch.delenv("GROK_HOME", raising=False)
        dest = prepare_runtime_grok_home(
            "http://127.0.0.1:1/mcp", home=home, workdir=work,
        )
        assert dest == home
        assert Path(os.environ["GROK_HOME"]).resolve() == home.resolve()
        assert (work / ".grok" / "config.toml").is_file()
        write_project_grok_config(work, "http://other/mcp")
        assert "http://other/mcp" in (work / ".grok" / "config.toml").read_text(encoding="utf-8")


class TestMcpListHealth:
    def test_accepts_rle_only(self) -> None:
        check_mcp_list_output("Configured MCP servers:\n  rle  http://127.0.0.1:1/mcp\n")

    def test_rejects_missing_rle(self) -> None:
        with pytest.raises(ValueError, match="did not list rle"):
            check_mcp_list_output("No MCP servers configured")

    def test_rejects_compat_zoo(self) -> None:
        with pytest.raises(ValueError, match="unexpected compatibility"):
            check_mcp_list_output("rle\nwandb  https://api.wandb.ai")


class TestDockerCliParser:
    def test_smoke_and_cal_flags(self) -> None:
        parser = build_parser()
        smoke = parser.parse_args(["smoke", "--call-tool", "--mcp-url", "http://x/mcp"])
        assert smoke.command == DockerCommand.SMOKE.value
        assert smoke.call_tool and smoke.mcp_url == "http://x/mcp"
        cal = parser.parse_args(
            ["cal", "--ticks", "3", "--scenario", "crashlanded", "--rimapi-url", "http://rimworld:8765"],
        )
        assert cal.command == DockerCommand.CAL.value
        assert cal.ticks == 3
        assert cal.rimapi_url == "http://rimworld:8765"

    def test_find_scenario_crashlanded(self) -> None:
        scenario = find_scenario("crashlanded")
        assert "crash" in scenario.name.lower() or "crashlanded" in scenario.name.lower()

    def test_find_scenario_missing(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="not found"):
            find_scenario("definitely-missing", definitions_dir=tmp_path)
