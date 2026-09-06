"""Exercise grok-docker.sh mount policy with a fake docker CLI (no daemon)."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from rle_harness_grok_build.argv_json import ARGV_JSON_ENV, write_argv_json
from rle_harness_grok_build.persist import (
    ACP_PUBLISH_ENV,
    ACP_SECRET_ENV,
    ACP_SERVE_ARG,
    PERSIST_ACTION_ENV,
    PERSIST_CONTAINER_ENV,
    PERSIST_KEEPALIVE_ARG,
)

WRAPPER = Path(__file__).resolve().parents[1] / "docker" / "grok-docker.sh"
PS1_WRAPPER = Path(__file__).resolve().parents[1] / "docker" / "grok-docker.ps1"


def _fake_docker_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "info" ]; then exit 0; fi\n'
        "printf '%s\\n' \"$@\"\n",
        encoding="utf-8",
    )
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def _run_wrapper(
    tmp_path: Path,
    args: list[str],
    extra_env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = f"{_fake_docker_bin(tmp_path)}{os.pathsep}{env.get('PATH', '')}"
    # Never forward real secrets into the fake docker argv dump.
    for key in (
        "XAI_API_KEY",
        "GROK_HOME",
        "GROK_AUTH_JSON",
        "GROK_DOCKER_HOME_VOLUME",
        "GROK_DOCKER_IMAGE",
        "MCP_URL",
        ARGV_JSON_ENV,
        PERSIST_ACTION_ENV,
        PERSIST_CONTAINER_ENV,
        ACP_PUBLISH_ENV,
        ACP_SECRET_ENV,
        "GROK_ACP_BIND",
    ):
        env.pop(key, None)
    env.update(extra_env)
    return subprocess.run(
        [str(WRAPPER), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _volume_targets(stdout: str) -> list[str]:
    parts = stdout.split()
    mounts: list[str] = []
    for i, part in enumerate(parts):
        if part == "-v" and i + 1 < len(parts):
            mounts.append(parts[i + 1])
    return mounts


class TestGrokDockerShHostileTemp:
    def test_skips_appdata_temp_grok_home(self, tmp_path: Path) -> None:
        home = tmp_path / "AppData" / "Local" / "Temp" / "rle-grok-home-abc"
        home.mkdir(parents=True)
        proc = _run_wrapper(tmp_path, ["mcp", "list"], {"GROK_HOME": str(home)})
        assert proc.returncode == 0, proc.stderr
        mounts = _volume_targets(proc.stdout)
        assert not any(m.endswith(":/home/grok/.grok") for m in mounts)
        assert "MCP_URL=http://host.docker.internal:8766/mcp" in proc.stdout
        assert "GROK_HOME=/home/grok/.grok" in proc.stdout
        assert "hostile temp GROK_HOME" in proc.stderr

    def test_skips_temp_rle_grok_cwd(self, tmp_path: Path) -> None:
        cwd = tmp_path / "Windows" / "Temp" / "rle-grok-xyz"
        cwd.mkdir(parents=True)
        proc = _run_wrapper(tmp_path, ["--cwd", str(cwd), "mcp", "list"], {})
        assert proc.returncode == 0, proc.stderr
        mounts = _volume_targets(proc.stdout)
        assert not any(m.endswith(":/work") for m in mounts)
        assert "--cwd" in proc.stdout.split()
        assert "/work" in proc.stdout.split()
        assert "hostile temp --cwd" in proc.stderr

    def test_mounts_unix_tmp_rle_grok_home(self, tmp_path: Path) -> None:
        """Linux /tmp/rle-grok* is fine; only Windows Temp\\rle-grok* is skipped."""
        home = tmp_path / "tmp" / "rle-grok-home-linux"
        home.mkdir(parents=True)
        proc = _run_wrapper(tmp_path, ["mcp", "list"], {"GROK_HOME": str(home)})
        assert proc.returncode == 0, proc.stderr
        mounts = _volume_targets(proc.stdout)
        assert f"{home}:/home/grok/.grok" in mounts
        assert "hostile temp" not in proc.stderr

    def test_mounts_non_temp_home_and_cwd(self, tmp_path: Path) -> None:
        home = tmp_path / "safe-grok-home"
        cwd = tmp_path / "safe-work"
        home.mkdir()
        cwd.mkdir()
        proc = _run_wrapper(
            tmp_path,
            ["--cwd", str(cwd), "mcp", "list"],
            {"GROK_HOME": str(home)},
        )
        assert proc.returncode == 0, proc.stderr
        mounts = _volume_targets(proc.stdout)
        assert f"{home}:/home/grok/.grok" in mounts
        assert f"{cwd}:/work" in mounts
        assert "hostile temp" not in proc.stderr

    def test_named_volume_wins_over_hostile_home(self, tmp_path: Path) -> None:
        home = tmp_path / "AppData" / "Local" / "Temp" / "rle-grok-home-vol"
        home.mkdir(parents=True)
        proc = _run_wrapper(
            tmp_path,
            ["mcp", "list"],
            {
                "GROK_HOME": str(home),
                "GROK_DOCKER_HOME_VOLUME": "rle-grok-home",
            },
        )
        assert proc.returncode == 0, proc.stderr
        mounts = _volume_targets(proc.stdout)
        assert "rle-grok-home:/home/grok/.grok" in mounts
        assert "hostile temp GROK_HOME" not in proc.stderr

    def test_refuses_host_plugin_home(self, tmp_path: Path) -> None:
        fake_home = tmp_path / "userhome"
        plugin = fake_home / ".grok"
        plugin.mkdir(parents=True)
        proc = _run_wrapper(
            tmp_path,
            ["mcp", "list"],
            {"GROK_HOME": str(plugin), "HOME": str(fake_home)},
        )
        assert proc.returncode == 0, proc.stderr
        mounts = _volume_targets(proc.stdout)
        assert not any(m.endswith(":/home/grok/.grok") for m in mounts)
        assert "refusing to mount host ~/.grok" in proc.stderr


class TestGrokDockerShArgvJson:
    def test_loads_sidecar_and_ignores_cli_args(self, tmp_path: Path) -> None:
        prompt = 'RLE turn — tick 0: do "the thing" ' + ("colonist " * 80)
        sidecar = write_argv_json(
            ["-p", prompt, "--output-format", "json", "--yolo"],
            tmp_path / "argv.json",
        )
        proc = _run_wrapper(
            tmp_path,
            ["this-cli-should-be-ignored", "mcp", "list"],
            {ARGV_JSON_ENV: str(sidecar)},
        )
        assert proc.returncode == 0, proc.stderr
        lines = proc.stdout.splitlines()
        assert "-p" in lines
        assert prompt in lines
        assert "--output-format" in lines
        assert "json" in lines
        assert "this-cli-should-be-ignored" not in lines
        assert "mcp" not in lines

    def test_sidecar_plus_hostile_temp_home_still_skips_mount(self, tmp_path: Path) -> None:
        home = tmp_path / "AppData" / "Local" / "Temp" / "rle-grok-home-json"
        home.mkdir(parents=True)
        sidecar = write_argv_json(["mcp", "list"], tmp_path / "health.json")
        proc = _run_wrapper(
            tmp_path,
            [],
            {ARGV_JSON_ENV: str(sidecar), "GROK_HOME": str(home)},
        )
        assert proc.returncode == 0, proc.stderr
        mounts = _volume_targets(proc.stdout)
        assert not any(m.endswith(":/home/grok/.grok") for m in mounts)
        assert "hostile temp GROK_HOME" in proc.stderr
        lines = proc.stdout.splitlines()
        assert "mcp" in lines and "list" in lines

    def test_missing_sidecar_file_fails(self, tmp_path: Path) -> None:
        proc = _run_wrapper(
            tmp_path,
            ["mcp", "list"],
            {ARGV_JSON_ENV: str(tmp_path / "missing.json")},
        )
        assert proc.returncode != 0
        assert "file not found" in proc.stderr


class TestGrokDockerPs1Invoke:
    def test_uses_splat_not_start_process(self) -> None:
        text = PS1_WRAPPER.read_text(encoding="utf-8")
        assert "& docker @runArgs" in text
        assert "& docker @execArgs" in text
        assert "& docker @startArgs" in text
        assert "RLE_GROK_PERSIST_ACTION" in text
        assert "RLE_GROK_ACP_PUBLISH" in text
        assert "acp-serve" in text
        assert "GROK_AGENT_SECRET" in text
        assert "/entrypoint.sh" in text
        assert "RLE_GROK_DOCKER_TRACE" in text
        invoke_lines = [
            line.strip()
            for line in text.splitlines()
            if not line.lstrip().startswith("#") and "Start-Process" in line
        ]
        assert invoke_lines == []


class TestGrokDockerShPersist:
    def test_start_is_detached_named_no_rm(self, tmp_path: Path) -> None:
        home = tmp_path / "safe-grok-home"
        cwd = tmp_path / "safe-work"
        home.mkdir()
        cwd.mkdir()
        proc = _run_wrapper(
            tmp_path,
            ["--cwd", str(cwd), PERSIST_KEEPALIVE_ARG],
            {
                "GROK_HOME": str(home),
                PERSIST_ACTION_ENV: "start",
                PERSIST_CONTAINER_ENV: "rle-grok-test1",
            },
        )
        assert proc.returncode == 0, proc.stderr
        lines = proc.stdout.splitlines()
        assert "run" in lines
        assert "-d" in lines
        assert "--name" in lines
        assert "rle-grok-test1" in lines
        assert "--rm" not in lines
        assert PERSIST_KEEPALIVE_ARG in lines
        mounts = _volume_targets(proc.stdout)
        assert f"{home}:/home/grok/.grok" in mounts
        assert f"{cwd}:/work" in mounts

    def test_exec_uses_entrypoint_and_rewrites_cwd(self, tmp_path: Path) -> None:
        cwd = tmp_path / "safe-work"
        cwd.mkdir()
        sidecar = write_argv_json(
            ["-p", 'RLE turn — tick 0', "--cwd", str(cwd), "--yolo"],
            tmp_path / "tick.json",
        )
        proc = _run_wrapper(
            tmp_path,
            [],
            {
                ARGV_JSON_ENV: str(sidecar),
                PERSIST_ACTION_ENV: "exec",
                PERSIST_CONTAINER_ENV: "rle-grok-test1",
            },
        )
        assert proc.returncode == 0, proc.stderr
        lines = proc.stdout.splitlines()
        assert lines[0] == "exec"
        assert "/entrypoint.sh" in lines
        assert "rle-grok-test1" in lines
        assert "-p" in lines
        assert "RLE turn — tick 0" in lines
        assert "--cwd" in lines
        assert "/work" in lines
        assert str(cwd) not in lines
        assert "--rm" not in lines
        assert "run" not in lines

    def test_stop_rm_container(self, tmp_path: Path) -> None:
        proc = _run_wrapper(
            tmp_path,
            ["ignored"],
            {
                PERSIST_ACTION_ENV: "stop",
                PERSIST_CONTAINER_ENV: "rle-grok-test1",
            },
        )
        assert proc.returncode == 0, proc.stderr
        text = proc.stdout
        assert "stop" in text.split()
        assert "rle-grok-test1" in text
        assert "rm" in text.split()

    def test_named_volume_on_start(self, tmp_path: Path) -> None:
        proc = _run_wrapper(
            tmp_path,
            [PERSIST_KEEPALIVE_ARG],
            {
                "GROK_DOCKER_HOME_VOLUME": "rle-grok-home",
                PERSIST_ACTION_ENV: "start",
                PERSIST_CONTAINER_ENV: "rle-grok-vol",
            },
        )
        assert proc.returncode == 0, proc.stderr
        assert "rle-grok-home:/home/grok/.grok" in _volume_targets(proc.stdout)
        assert "--rm" not in proc.stdout.split()

    def test_unknown_action_fails(self, tmp_path: Path) -> None:
        proc = _run_wrapper(
            tmp_path,
            ["mcp", "list"],
            {PERSIST_ACTION_ENV: "pause", PERSIST_CONTAINER_ENV: "x"},
        )
        assert proc.returncode != 0
        assert "unknown RLE_GROK_PERSIST_ACTION" in proc.stderr

    def test_entrypoint_persist_keepalive(self) -> None:
        text = (Path(__file__).resolve().parents[1] / "docker" / "entrypoint.sh").read_text(
            encoding="utf-8",
        )
        assert '[[ "${1:-}" == "persist" ]]' in text
        assert "sleep infinity" in text

    def test_start_acp_serve_publishes_port(self, tmp_path: Path) -> None:
        cwd = tmp_path / "safe-work"
        cwd.mkdir()
        proc = _run_wrapper(
            tmp_path,
            ["--cwd", str(cwd), "-m", "grok-4.6", ACP_SERVE_ARG],
            {
                PERSIST_ACTION_ENV: "start",
                PERSIST_CONTAINER_ENV: "rle-grok-acp",
                ACP_PUBLISH_ENV: "127.0.0.1:2419:2419",
                ACP_SECRET_ENV: "tok",
            },
        )
        assert proc.returncode == 0, proc.stderr
        lines = proc.stdout.splitlines()
        assert "run" in lines
        assert "-d" in lines
        assert "-p" in lines
        assert "127.0.0.1:2419:2419" in lines
        assert "GROK_AGENT_SECRET=tok" in lines
        assert ACP_SERVE_ARG in lines
        assert PERSIST_KEEPALIVE_ARG not in lines
        assert "--rm" not in lines
        assert "-m" in lines
        assert "grok-4.6" in lines
        assert "--cwd" in lines
        assert "/work" in lines

    def test_entrypoint_acp_serve(self) -> None:
        text = (Path(__file__).resolve().parents[1] / "docker" / "entrypoint.sh").read_text(
            encoding="utf-8",
        )
        assert '[[ "${1:-}" == "acp-serve" ]]' in text
        assert "grok agent --always-approve" in text
        assert 'serve --bind "$bind" --secret "$GROK_AGENT_SECRET"' in text
        assert "GROK_AGENT_SECRET" in text
