"""rle-harness-grok-build — Grok Build as an RLE harness.

Grok Build (https://github.com/xai-org/grok-build, Apache-2.0) is xAI's
terminal coding agent with a scriptable headless mode and native MCP support.
This package registers it with RLE (https://github.com/AppSprout-dev/RLE) so

    python scripts/run_benchmark.py --harness grok-build --model grok-4.6

benchmarks *Grok Build as the harness* on the same scenarios, saves and
scoring as every other harness. Each tick is one headless invocation
(``grok -p ... --output-format json``) resuming the previous session; the
agent acts through the RLE MCP tools and calls ``rle__end_turn``.
"""

from rle_harness_grok_build.plugin import PLUGIN, GrokBuildPlugin

__all__ = ["PLUGIN", "GrokBuildPlugin"]
