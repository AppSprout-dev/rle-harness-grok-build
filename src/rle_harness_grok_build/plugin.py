"""Entry point for ``--harness grok-build``."""

from __future__ import annotations

import shutil

from pydantic import BaseModel
from rle.harness import Availability, BaseHarness, HarnessContext
from rle.testing.scripted_agent import ScriptedMcpHarness

from rle_harness_grok_build.harness import GrokBuildHarness, binary_version
from rle_harness_grok_build.isolated_home import resolve_binary
from rle_harness_grok_build.options import GrokBuildOptions


class GrokBuildPlugin:
    name = "grok-build"
    description = (
        "Grok Build coding agent (headless `grok -p`, session resumed each tick) acting "
        "through the RLE MCP tools."
    )

    def available(self) -> Availability:
        if resolve_binary("grok") is not None:
            return Availability.available()
        # Windows escape hatch: stock Linux grok via docker/grok-docker.cmd
        if shutil.which("docker") is not None:
            return Availability.available()
        return Availability.missing(
            "grok binary not on PATH and docker not found. "
            "Install grok (curl -fsSL https://x.ai/cli/install.sh | bash) "
            "or Docker Desktop + docker/grok-docker.sh|.cmd "
            "(--harness-opt binary=...).",
        )

    def option_schema(self) -> type[BaseModel]:
        return GrokBuildOptions

    def create(self, ctx: HarnessContext, options: BaseModel) -> BaseHarness:
        assert isinstance(options, GrokBuildOptions)
        if resolve_binary(options.binary) is None:
            raise RuntimeError(
                f"Grok Build binary {options.binary!r} not found; set --harness-opt "
                "binary=docker/grok-docker.sh (or grok-docker.cmd on Windows)",
            )
        return GrokBuildHarness(options)

    def smoke(self, ctx: HarnessContext, options: BaseModel) -> BaseHarness:
        """No Grok Build needed: a scripted agent plays the MCP round trip."""
        assert isinstance(options, GrokBuildOptions)
        return ScriptedMcpHarness(options, name=self.name)

    def describe(self) -> dict[str, str]:
        return {"harness": self.name, "grok-build": binary_version("grok")}


PLUGIN = GrokBuildPlugin()
