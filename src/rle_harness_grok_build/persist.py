"""Persistent Docker lifecycle for the warm/OpenCode-parity spike.

Grok Build has no OpenCode-style HTTP serve. Documented long-lived modes are
ACP (``grok agent serve`` / ``grok agent stdio``) — not used here. Warm mode
starts one sidecar container in ``start_agent``, ``docker exec`` each tick
(``grok -p --resume``), and stops the container in teardown.

Wrappers read:

* ``RLE_GROK_PERSIST_CONTAINER`` — ``docker run --name`` / ``docker exec`` target
* ``RLE_GROK_PERSIST_ACTION`` — ``start`` | ``exec`` | ``stop``
"""

from __future__ import annotations

import uuid
from typing import Literal

PERSIST_CONTAINER_ENV = "RLE_GROK_PERSIST_CONTAINER"
PERSIST_ACTION_ENV = "RLE_GROK_PERSIST_ACTION"
PERSIST_ACTIONS = frozenset({"start", "exec", "stop"})
PersistAction = Literal["start", "exec", "stop"]

# docker exec target: rewrite isolated config, then grok. Not the image ENTRYPOINT
# automatically — docker exec skips it — so wrappers call this path explicitly.
CONTAINER_ENTRYPOINT = "/entrypoint.sh"
PERSIST_KEEPALIVE_ARG = "persist"


def new_container_name() -> str:
    """Stable-enough unique name: ``rle-grok-`` + 12 hex chars."""
    return f"rle-grok-{uuid.uuid4().hex[:12]}"


def persist_start_args(binary: str, workdir: str) -> list[str]:
    """Wrapper argv so start can bind-mount ``--cwd`` then run ``persist``."""
    return [binary, "--cwd", workdir, PERSIST_KEEPALIVE_ARG]


def apply_persist_env(
    env: dict[str, str],
    *,
    container: str,
    action: PersistAction,
) -> dict[str, str]:
    """Stamp persist env onto a child environment (mutates and returns *env*)."""
    if action not in PERSIST_ACTIONS:
        raise ValueError(f"unknown persist action: {action!r}")
    if not container:
        raise ValueError("persist container name is required")
    env[PERSIST_CONTAINER_ENV] = container
    env[PERSIST_ACTION_ENV] = action
    return env
