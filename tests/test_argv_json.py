"""Unit tests for the grok-docker argv JSON side-channel (no grok, no Docker)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rle_harness_grok_build.argv_json import (
    ARGV_JSON_ENV,
    is_docker_wrapper_binary,
    load_argv_json,
    prepare_docker_wrapper_invocation,
    write_argv_json,
)


class TestIsDockerWrapperBinary:
    def test_matches_wrapper_basenames(self) -> None:
        assert is_docker_wrapper_binary(r"C:\repo\docker\grok-docker.cmd")
        assert is_docker_wrapper_binary("./docker/grok-docker.ps1")
        assert is_docker_wrapper_binary("/opt/rle/docker/grok-docker.sh")
        assert is_docker_wrapper_binary("GROK-DOCKER.CMD")

    def test_ignores_stock_grok(self) -> None:
        assert not is_docker_wrapper_binary("grok")
        assert not is_docker_wrapper_binary("/usr/local/bin/grok")
        assert not is_docker_wrapper_binary(r"C:\Users\me\grok.exe")
        assert not is_docker_wrapper_binary("grok-docker")
        assert not is_docker_wrapper_binary("not-grok-docker.sh")


class TestArgvJsonRoundTrip:
    def test_write_load_preserves_quoted_and_huge_prompt(self, tmp_path: Path) -> None:
        prompt = 'Crashlanded: do "the thing"\n' + ("colonist " * 400)
        args = ["-p", prompt, "--output-format", "json", "--cwd", r"C:\Temp\rle-grok-abc"]
        dest = tmp_path / "argv.json"
        path = write_argv_json(args, dest)
        assert path == dest
        assert load_argv_json(path) == args
        raw = dest.read_text(encoding="utf-8")
        assert '"-p"' in raw
        assert "the thing" in raw

    def test_rejects_non_array(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text('{"-p": "nope"}', encoding="utf-8")
        with pytest.raises(ValueError, match="JSON array of strings"):
            load_argv_json(bad)

    def test_rejects_non_string_items(self, tmp_path: Path) -> None:
        bad = tmp_path / "nums.json"
        bad.write_text("[1, 2]", encoding="utf-8")
        with pytest.raises(ValueError, match="JSON array of strings"):
            load_argv_json(bad)


class TestPrepareDockerWrapperInvocation:
    def test_writes_json_and_drops_cli_args_for_cmd(self, tmp_path: Path) -> None:
        dest = tmp_path / "sidecar.json"
        env: dict[str, str] = {}
        binary = r"C:\repo\docker\grok-docker.cmd"
        prompt = "huge prompt with spaces and \"quotes\""
        cmd = [binary, "-p", prompt, "--output-format", "json", "--yolo"]
        invoke, sidecar = prepare_docker_wrapper_invocation(cmd, env, dest=dest)
        assert invoke == [binary]
        assert sidecar == dest
        assert env[ARGV_JSON_ENV] == str(dest)
        assert load_argv_json(dest) == ["-p", prompt, "--output-format", "json", "--yolo"]

    def test_leaves_stock_grok_argv_on_cli(self, tmp_path: Path) -> None:
        env: dict[str, str] = {}
        cmd = ["/bin/grok", "-p", "do it", "--yolo"]
        invoke, sidecar = prepare_docker_wrapper_invocation(cmd, env, dest=tmp_path / "x.json")
        assert invoke == cmd
        assert sidecar is None
        assert ARGV_JSON_ENV not in env
        assert not (tmp_path / "x.json").exists()

    def test_healthcheck_mcp_list_also_uses_sidecar(self, tmp_path: Path) -> None:
        dest = tmp_path / "health.json"
        env: dict[str, str] = {}
        binary = "grok-docker.sh"
        invoke, sidecar = prepare_docker_wrapper_invocation(
            [binary, "mcp", "list"], env, dest=dest,
        )
        assert invoke == [binary]
        assert sidecar == dest
        assert load_argv_json(dest) == ["mcp", "list"]
