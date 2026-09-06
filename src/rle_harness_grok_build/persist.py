"""Persistent Docker lifecycle for warm ``grok -p`` and ACP ``agent serve``.

Warm mode starts one sidecar and ``docker exec``s ``grok -p --resume``.
ACP mode starts the same sidecar with entrypoint ``acp-serve`` (documented
``grok agent --always-approve serve``) and publishes the WebSocket port.

Wrappers read:

* ``RLE_GROK_PERSIST_CONTAINER`` — ``docker run --name`` / ``docker exec`` target
* ``RLE_GROK_PERSIST_ACTION`` — ``start`` | ``exec`` | ``stop``
* ``RLE_GROK_ACP_PUBLISH`` — ``docker run -p`` value (``host:port:2419``)
* ``GROK_AGENT_SECRET`` — serve auth token (also grok's documented env)
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Literal

PERSIST_CONTAINER_ENV = "RLE_GROK_PERSIST_CONTAINER"
PERSIST_ACTION_ENV = "RLE_GROK_PERSIST_ACTION"
ACP_PUBLISH_ENV = "RLE_GROK_ACP_PUBLISH"
ACP_SECRET_ENV = "GROK_AGENT_SECRET"
PERSIST_ACTIONS = frozenset({"start", "exec", "stop"})
PersistAction = Literal["start", "exec", "stop"]

# docker exec target: rewrite isolated config, then grok. Not the image ENTRYPOINT
# automatically — docker exec skips it — so wrappers call this path explicitly.
CONTAINER_ENTRYPOINT = "/entrypoint.sh"
PERSIST_KEEPALIVE_ARG = "persist"
ACP_SERVE_ARG = "acp-serve"


def new_container_name() -> str:
    """Stable-enough unique name: ``rle-grok-`` + 12 hex chars."""
    return f"rle-grok-{uuid.uuid4().hex[:12]}"


def persist_start_args(
    binary: str,
    workdir: str,
    *,
    acp: bool = False,
    agent_flags: Sequence[str] | None = None,
) -> list[str]:
    """Wrapper argv so start can bind-mount ``--cwd`` then run persist/ACP serve."""
    cmd = [binary, "--cwd", workdir]
    if acp:
        cmd.extend(agent_flags or ())
        cmd.append(ACP_SERVE_ARG)
        return cmd
    cmd.append(PERSIST_KEEPALIVE_ARG)
    return cmd


def apply_acp_env(
    env: dict[str, str],
    *,
    publish: str,
    secret: str,
) -> dict[str, str]:
    """Stamp ACP publish/secret onto a child environment (mutates *env*)."""
    if not publish:
        raise ValueError("ACP publish spec is required")
    if not secret:
        raise ValueError("ACP secret is required")
    env[ACP_PUBLISH_ENV] = publish
    env[ACP_SECRET_ENV] = secret
    return env


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
