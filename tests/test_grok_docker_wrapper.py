"""Exercise grok-docker.sh mount policy with a fake docker CLI (no daemon)."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from rle_harness_grok_build.argv_json import ARGV_JSON_ENV, write_argv_json

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
        assert "& docker @dockerArgs" in text
        assert "RLE_GROK_DOCKER_TRACE" in text
        invoke_lines = [
            line.strip()
            for line in text.splitlines()
            if not line.lstrip().startswith("#") and "Start-Process" in line
        ]
        assert invoke_lines == []
