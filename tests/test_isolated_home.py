"""Unit tests for isolated GROK_HOME / Docker config helpers (no grok, no RimWorld)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rle_harness_grok_build.harness import mcp_config_toml as harness_mcp_config_toml
from rle_harness_grok_build.isolated_home import (
    DEFAULT_ADVERTISED_MCP_URL,
    DEFAULT_HOST_RIMAPI_URL,
    FORBIDDEN_HOST_BIND_SOURCES,
    check_mcp_list_output,
    effective_mcp_url,
    is_host_plugin_home,
    mcp_config_toml,
    prepare_runtime_grok_home,
    resolve_auth_json,
    resolve_binary,
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
    def test_rimapi_is_host_localhost(self) -> None:
        assert resolve_rimapi_url("http://127.0.0.1:8765/", env={"RIMAPI_URL": "http://other"}) == (
            "http://127.0.0.1:8765"
        )
        assert resolve_rimapi_url(None, env={"RIMAPI_URL": "http://host:8765/"}) == "http://host:8765"
        assert resolve_rimapi_url(None, env={}) == DEFAULT_HOST_RIMAPI_URL

    def test_mcp_url_defaults_to_advertised(self) -> None:
        assert resolve_mcp_url("http://127.0.0.1:1/mcp", env={"MCP_URL": "x"}) == (
            "http://127.0.0.1:1/mcp"
        )
        assert resolve_mcp_url(None, env={"MCP_URL": "http://in/mcp"}) == "http://in/mcp"
        assert resolve_mcp_url(None, env={}) == DEFAULT_ADVERTISED_MCP_URL
        assert DEFAULT_ADVERTISED_MCP_URL == "http://host.docker.internal:8766/mcp"

    def test_effective_mcp_url_advertise_wins(self) -> None:
        bind = "http://127.0.0.1:54321/mcp"
        adv = "http://host.docker.internal:8766/mcp"
        assert effective_mcp_url(bind, None) == bind
        assert effective_mcp_url(bind, adv) == adv


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

    def test_is_host_plugin_home(self, tmp_path: Path) -> None:
        assert is_host_plugin_home(Path.home() / ".grok")
        assert not is_host_plugin_home(tmp_path / "rle-grok-home-xyz")


class TestResolveBinary:
    def test_direct_path(self, tmp_path: Path) -> None:
        script = tmp_path / "grok-docker.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        script.chmod(0o755)
        assert resolve_binary(str(script)) == str(script.resolve())

    def test_missing(self) -> None:
        assert resolve_binary("definitely-not-a-grok-binary-9cb3") is None


class TestWriteIsolatedHome:
    def test_writes_config_and_optional_auth_only(self, tmp_path: Path) -> None:
        home = tmp_path / "grok-home"
        auth = tmp_path / "auth.json"
        auth.write_text('{"token":"x"}', encoding="utf-8")
        write_isolated_grok_home(home, "http://127.0.0.1:9/mcp", auth_json=auth)
        written = (home / "config.toml").read_text(encoding="utf-8")
        assert written == mcp_config_toml("http://127.0.0.1:9/mcp")
        assert (home / "auth.json").read_text(encoding="utf-8") == '{"token":"x"}'
        assert not (home / "plugins").exists()
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
