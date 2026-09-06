"""Isolated GROK_HOME helpers shared by the harness and the stock Docker path.

The host Windows hang (180s, 0 tokens) is hypothesized to be environment
pollution: desktop grok loads Claude/Cursor compat MCPs and plugins even after
a temp GROK_HOME. The Linux container writes a *fresh* home that contains only
the RLE MCP stanza plus optional ``auth.json``. It never copies host
``~/.grok/config.toml``, ``~/.claude.json``, or the plugin zoo.

Architecture (locked):

* Windows RimWorld + RIMAPI stay on the **host** (``localhost:8765``).
* Container is stock Linux grok only.
* Grok reaches RLE's MCP at the **advertised** URL
  ``http://host.docker.internal:8766/mcp`` (requires an RLE McpHost that
  binds ``0.0.0.0`` on a fixed port — sibling RLE PR). Today's McpHost binds
  ``127.0.0.1`` + ephemeral port and is unreachable from Docker.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping
from pathlib import Path

MCP_SERVER_NAME = "rle"
COMPAT_MCP_NAMES = ("wandb", "claude", "cursor")

# Auth files the container *may* ingest. Never config.toml / plugins.
AUTH_FILENAMES = ("auth.json", "mcp_credentials.json")

# Preferred in-container mount for a *host-exported* auth.json (file only).
CONTAINER_AUTH_MOUNT = Path("/auth/auth.json")

# Host Windows RimWorld (not run inside the Linux grok container).
DEFAULT_HOST_RIMAPI_URL = "http://127.0.0.1:8765"
DEFAULT_MCP_LISTEN_PORT = 8766
# What grok-in-Docker must dial after the RLE McpHost bind/advertise PR.
DEFAULT_ADVERTISED_MCP_URL = "http://host.docker.internal:8766/mcp"
DEFAULT_CONTAINER_GROK_HOME = Path("/home/grok/.grok")

# Documented host paths that must not be bind-mounted into the image.
FORBIDDEN_HOST_BIND_SOURCES = (
    "~/.grok",
    "~/.grok/config.toml",
    "~/.claude.json",
    "~/.claude",
    "~/.cursor",
)

_POLLUTION_NAMES = (
    "plugins",
    "skills",
    "mcp.json",
    "managed_config.toml",
    "requirements.toml",
)


def mcp_config_toml(mcp_url: str) -> str:
    """RLE-only MCP config with compatibility MCP imports disabled."""
    return (
        f"[mcp_servers.{MCP_SERVER_NAME}]\n"
        f'url = "{mcp_url}"\n'
        f"startup_timeout_sec = 30\n"
        f'headers = {{ "x-mcp-session-id" = "{{{{session_id}}}}" }}\n'
        "\n"
        "[compat.claude]\n"
        "mcps = false\n"
        "\n"
        "[compat.cursor]\n"
        "mcps = false\n"
    )


def effective_mcp_url(bind_url: str, advertise_url: str | None) -> str:
    """URL written into grok config: advertised (Docker) wins over bind URL."""
    if advertise_url:
        return advertise_url
    return bind_url


def resolve_rimapi_url(
    explicit: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Host RimAPI base URL: flag, then ``RIMAPI_URL``, then localhost:8765."""
    if explicit:
        return explicit.rstrip("/")
    environ = os.environ if env is None else env
    from_env = environ.get("RIMAPI_URL")
    if from_env:
        return from_env.rstrip("/")
    return DEFAULT_HOST_RIMAPI_URL


def resolve_mcp_url(
    explicit: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Advertised MCP URL written into isolated ``config.toml``."""
    if explicit:
        return explicit
    environ = os.environ if env is None else env
    from_env = environ.get("MCP_URL")
    if from_env:
        return from_env
    return DEFAULT_ADVERTISED_MCP_URL


def resolve_auth_json(
    explicit: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path | None:
    """Optional auth.json: flag, ``GROK_AUTH_JSON``, then ``/auth/auth.json``."""
    if explicit is not None:
        return explicit
    environ = os.environ if env is None else env
    from_env = environ.get("GROK_AUTH_JSON")
    if from_env:
        return Path(from_env)
    if CONTAINER_AUTH_MOUNT.is_file() and CONTAINER_AUTH_MOUNT.stat().st_size > 0:
        return CONTAINER_AUTH_MOUNT
    return None


def resolve_binary(binary: str) -> str | None:
    """Resolve a grok executable or wrapper script (``.sh`` / ``.cmd`` / ``.ps1``)."""
    candidate = Path(binary).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())
    return shutil.which(binary)


def is_host_plugin_home(home: Path) -> bool:
    """True when *home* is the user's real ``~/.grok`` (must not be mounted)."""
    try:
        return home.expanduser().resolve() == (Path.home() / ".grok").resolve()
    except OSError:
        return False


def wipe_plugin_pollution(home: Path) -> list[str]:
    """Remove leftover plugin/skill trees so a dirty volume cannot leak MCPs."""
    removed: list[str] = []
    for name in _POLLUTION_NAMES:
        path = home / name
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(name)
        elif path.is_file():
            path.unlink()
            removed.append(name)
    return removed


def write_isolated_grok_home(
    home: Path,
    mcp_url: str,
    *,
    auth_json: Path | None = None,
) -> Path:
    """Create an empty-ish GROK_HOME with RLE-only ``config.toml``.

    Copies ``auth.json`` (and only that file) when *auth_json* is a non-empty
    file. Never reads host ``config.toml`` or Claude/Cursor MCP configs.
    """
    home.mkdir(parents=True, exist_ok=True)
    wipe_plugin_pollution(home)
    (home / "config.toml").write_text(mcp_config_toml(mcp_url), encoding="utf-8")
    if auth_json is not None and auth_json.is_file() and auth_json.stat().st_size > 0:
        shutil.copy2(auth_json, home / "auth.json")
    return home


def write_project_grok_config(workdir: Path, mcp_url: str) -> Path:
    """Project-scoped ``.grok/config.toml`` (cwd priority / defense in depth)."""
    cfg_dir = workdir / ".grok"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / "config.toml"
    path.write_text(mcp_config_toml(mcp_url), encoding="utf-8")
    return path


def prepare_runtime_grok_home(
    mcp_url: str,
    *,
    home: Path | None = None,
    auth_json: Path | None = None,
    workdir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Write isolated home, optional project config, and export ``GROK_HOME``."""
    environ = os.environ if env is None else env
    dest = home
    if dest is None:
        env_home = environ.get("GROK_HOME")
        dest = Path(env_home) if env_home else Path.home() / ".grok"
    resolved_auth = resolve_auth_json(auth_json, env=environ)
    write_isolated_grok_home(dest, mcp_url, auth_json=resolved_auth)
    if workdir is not None:
        write_project_grok_config(workdir, mcp_url)
    if environ is os.environ:
        os.environ["GROK_HOME"] = str(dest)
    return dest


def check_mcp_list_output(output: str) -> None:
    """Raise ``ValueError`` unless *output* lists rle and no compatibility MCPs."""
    if not any(re.search(r"\brle\b", line, re.IGNORECASE) for line in output.splitlines()):
        raise ValueError(f"grok MCP healthcheck did not list rle: {output.strip()[-800:]}")
    compat = [
        name
        for name in COMPAT_MCP_NAMES
        if re.search(rf"\b{name}\b", output, re.IGNORECASE)
    ]
    if compat:
        raise ValueError(
            "grok MCP healthcheck found unexpected compatibility MCPs: " + ", ".join(compat),
        )
