"""Unit tests for isolated GROK_HOME / Docker config helpers (no grok, no RimWorld)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rle_harness_grok_build.harness import mcp_config_toml as harness_mcp_config_toml
from rle_harness_grok_build.isolated_home import (
    DEFAULT_ADVERTISED_MCP_URL,
    DEFAULT_API_BACKEND,
    DEFAULT_HOST_RIMAPI_URL,
    DEFAULT_OPENROUTER_API_KEY_ENV,
    DEFAULT_OPENROUTER_BASE_URL,
    DEFAULT_XAI_API_KEY_ENV,
    FORBIDDEN_HOST_BIND_SOURCES,
    CustomModel,
    check_mcp_list_output,
    effective_mcp_url,
    is_host_plugin_home,
    is_hostile_temp_bind_source,
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
from rle_harness_grok_build.options import GrokBuildOptions


class TestMcpConfig:
    def test_rle_only_and_compat_off(self) -> None:
        toml = mcp_config_toml("http://127.0.0.1:7000/mcp")
        assert "[mcp_servers.rle]" in toml
        assert 'url = "http://127.0.0.1:7000/mcp"' in toml
        assert "[compat.claude]" in toml and "mcps = false" in toml
        assert "[compat.cursor]" in toml
        assert harness_mcp_config_toml("http://x") == mcp_config_toml("http://x")
        assert "[model." not in toml
        assert "openrouter" not in toml
        assert "base_url" not in toml
        assert "env_key" not in toml

    def test_does_not_mention_host_plugin_servers(self) -> None:
        toml = mcp_config_toml("http://rle/mcp")
        assert "wandb" not in toml
        assert "huggingface" not in toml

    def test_openrouter_model_keeps_mcp_and_writes_env_key(self) -> None:
        spec = CustomModel(
            model="google/gemini-3.8-flash",
            base_url=DEFAULT_OPENROUTER_BASE_URL,
            env_key=DEFAULT_OPENROUTER_API_KEY_ENV,
        )
        toml = mcp_config_toml("http://host.docker.internal:8766/mcp", spec)
        assert "[mcp_servers.rle]" in toml
        assert "[compat.claude]" in toml and "mcps = false" in toml
        assert "[compat.cursor]" in toml
        assert '[model."google/gemini-3.8-flash"]' in toml
        assert 'model = "google/gemini-3.8-flash"' in toml
        assert f'base_url = "{DEFAULT_OPENROUTER_BASE_URL}"' in toml
        assert f'env_key = "{DEFAULT_OPENROUTER_API_KEY_ENV}"' in toml
        assert f'api_backend = "{DEFAULT_API_BACKEND}"' in toml
        assert "HTTP-Referer" in toml
        assert "rle-harness-grok-build" in toml
        assert "sk-or-" not in toml
        assert "api_key =" not in toml

    def test_xai_default_path_unchanged_when_opts_off(self) -> None:
        assert GrokBuildOptions().resolve_custom_model("grok-4.6") is None
        assert mcp_config_toml("http://x") == mcp_config_toml("http://x", None)


class TestOpenRouterOptions:
    def test_openai_compat_defaults(self) -> None:
        opts = GrokBuildOptions(openai_compat=True)
        assert opts.openai_compat_enabled
        assert opts.effective_base_url == DEFAULT_OPENROUTER_BASE_URL
        assert opts.effective_api_key_env == DEFAULT_OPENROUTER_API_KEY_ENV
        spec = opts.resolve_custom_model("deepseek/deepseek-v4-flash-0731")
        assert spec is not None
        assert spec.model == "deepseek/deepseek-v4-flash-0731"
        assert spec.base_url == DEFAULT_OPENROUTER_BASE_URL
        assert spec.env_key == DEFAULT_OPENROUTER_API_KEY_ENV

    def test_provider_openrouter_alias(self) -> None:
        opts = GrokBuildOptions(provider="openrouter")
        assert opts.openai_compat_enabled
        spec = opts.resolve_custom_model("google/gemini-3.8-flash")
        assert spec is not None
        assert spec.env_key == DEFAULT_OPENROUTER_API_KEY_ENV

    def test_explicit_api_key_env_and_base_url(self) -> None:
        opts = GrokBuildOptions(
            openai_compat=True,
            api_key_env="MY_OR_KEY",
            base_url="https://openrouter.ai/api/v1/",
        )
        assert opts.effective_api_key_env == "MY_OR_KEY"
        assert opts.effective_base_url == "https://openrouter.ai/api/v1"
        spec = opts.resolve_custom_model("google/gemini-3.8-flash")
        assert spec is not None
        assert spec.env_key == "MY_OR_KEY"

    def test_xai_default_api_key_env_when_opts_off(self) -> None:
        opts = GrokBuildOptions()
        assert not opts.openai_compat_enabled
        assert opts.effective_api_key_env == DEFAULT_XAI_API_KEY_ENV
        assert opts.effective_base_url is None
        assert opts.acp_enabled is False


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


class TestHostileTempBindSource:
    def test_windows_appdata_temp_and_rle_prefixes(self) -> None:
        assert is_hostile_temp_bind_source(
            r"C:\Users\me\AppData\Local\Temp\rle-grok-home-abc",
        )
        assert is_hostile_temp_bind_source(
            "C:/Users/me/AppData/Local/Temp/rle-grok-xyz",
        )
        assert is_hostile_temp_bind_source(
            "/mnt/c/Users/me/AppData/Local/Temp/rle-grok-home-1",
        )
        assert is_hostile_temp_bind_source(r"D:\Scratch\Temp\rle-grok-home-2")
        assert is_hostile_temp_bind_source(r"C:\Windows\Temp\rle-grok-cwd")

    def test_unix_tmp_is_not_hostile(self, tmp_path: Path) -> None:
        assert not is_hostile_temp_bind_source("/tmp/rle-grok-home-abc")
        assert not is_hostile_temp_bind_source(tmp_path / "rle-grok-home-xyz")
        assert not is_hostile_temp_bind_source("/home/me/.grok")

    def test_windows_temp_env_prefix_only(self) -> None:
        env = {"TEMP": r"D:\Scratch", "TMP": r"D:\Scratch"}
        assert is_hostile_temp_bind_source(r"D:\Scratch\rle-grok-home-1", env=env)
        assert not is_hostile_temp_bind_source("/tmp/rle-grok-home-1", env={"TMPDIR": "/tmp"})
        assert not is_hostile_temp_bind_source("/tmp/foo", env={"TEMP": "/tmp"})


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
        assert "[model." not in written
        assert "openrouter" not in written

    def test_writes_openrouter_model_when_enabled(self, tmp_path: Path) -> None:
        home = tmp_path / "grok-home"
        spec = CustomModel(
            model="google/gemini-3.8-flash",
            base_url=DEFAULT_OPENROUTER_BASE_URL,
            env_key=DEFAULT_OPENROUTER_API_KEY_ENV,
        )
        write_isolated_grok_home(home, "http://127.0.0.1:9/mcp", custom_model=spec)
        written = (home / "config.toml").read_text(encoding="utf-8")
        assert written == mcp_config_toml("http://127.0.0.1:9/mcp", spec)
        assert "[mcp_servers.rle]" in written
        assert f'base_url = "{DEFAULT_OPENROUTER_BASE_URL}"' in written
        assert f'env_key = "{DEFAULT_OPENROUTER_API_KEY_ENV}"' in written
        assert "sk-" not in written

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
