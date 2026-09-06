"""Windows-safe argv side-channel for grok-docker wrappers.

``grok-docker.cmd`` launches PowerShell with ``%*``. cmd.exe drops quoted and
large ``-p`` prompts, so the wrapper starts grok with no args. The image
entrypoint then defaults to ``mcp list`` and exits 0 — Crashlanded ticks
report 0 tokens / 0 actions / success.

When the harness binary basename is a grok-docker wrapper, it writes the
argv (everything after the wrapper path) as UTF-8 JSON and sets
``RLE_GROK_ARGV_JSON``. Wrappers load that file when present; otherwise they
keep parsing the command line.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

ARGV_JSON_ENV = "RLE_GROK_ARGV_JSON"

DOCKER_WRAPPER_NAMES = frozenset({
    "grok-docker.cmd",
    "grok-docker.ps1",
    "grok-docker.sh",
})


def is_docker_wrapper_binary(binary: str) -> bool:
    """True when *binary* basename is a grok-docker wrapper script."""
    # Normalize Windows separators so ``C:\...\grok-docker.cmd`` matches on Unix.
    return Path(binary.replace("\\", "/")).name.lower() in DOCKER_WRAPPER_NAMES


def write_argv_json(args: Sequence[str], dest: Path | None = None) -> Path:
    """Write argv (after the binary) as a UTF-8 JSON array of strings."""
    if dest is None:
        fd, name = tempfile.mkstemp(prefix="rle-grok-argv-", suffix=".json")
        os.close(fd)
        dest = Path(name)
    dest.write_text(json.dumps(list(args), ensure_ascii=False), encoding="utf-8")
    return dest


def load_argv_json(path: Path | str) -> list[str]:
    """Load a UTF-8 JSON array of strings written by :func:`write_argv_json`."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise ValueError(f"{ARGV_JSON_ENV} must be a JSON array of strings")
    return data


def prepare_docker_wrapper_invocation(
    cmd: Sequence[str],
    env: dict[str, str],
    *,
    dest: Path | None = None,
) -> tuple[list[str], Path | None]:
    """If ``cmd[0]`` is a grok-docker wrapper, side-channel argv via JSON.

    Returns ``(exec_argv, sidecar_path)``. *exec_argv* is only the wrapper
    when a sidecar is written (no CLI grok args). *sidecar_path* is ``None``
    when the binary is a normal grok executable.
    """
    if not cmd:
        return [], None
    binary = cmd[0]
    if not is_docker_wrapper_binary(binary):
        return list(cmd), None
    path = write_argv_json(list(cmd[1:]), dest)
    env[ARGV_JSON_ENV] = str(path)
    return [binary], path
