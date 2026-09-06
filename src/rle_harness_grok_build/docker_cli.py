"""Stock-container entry: isolated GROK_HOME plus smoke or Crashlanded cal.

Design: grok + this harness + RLE live in one image. The in-process RLE MCP
is injected for the run; grok never sees host Claude/Cursor/plugin MCPs.
RimAPI is reached at a configurable URL (default
``http://host.docker.internal:8765``).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import subprocess
import sys
from enum import Enum
from pathlib import Path
from typing import assert_never

from rle.config import RLEConfig
from rle.docker import wait_for_rimapi
from rle.harness import HarnessContext, create_harness
from rle.harness.brief import build_brief
from rle.mcp.host import McpHost
from rle.mcp.ledger import TickLedger
from rle.mcp.server import build_server
from rle.mcp.session import McpSession
from rle.orchestration.action_executor import ActionExecutor
from rle.orchestration.game_loop import RLEGameLoop
from rle.orchestration.save_loader import load_save_and_settle
from rle.orchestration.state_manager import GameStateManager
from rle.rimapi.client import RimAPIClient
from rle.scenarios import loader as scenario_loader
from rle.scenarios.evaluator import ScenarioEvaluator
from rle.scenarios.loader import load_scenario
from rle.scenarios.schema import ScenarioConfig
from rle.scoring.composite import CompositeScorer
from rle.scoring.recorder import TimeSeriesRecorder
from rle.testing import MockRimAPI

from rle_harness_grok_build.isolated_home import (
    DEFAULT_COMPOSE_RIMWORLD_URL,
    DEFAULT_DOCKER_RIMAPI_URL,
    FORBIDDEN_HOST_BIND_SOURCES,
    check_mcp_list_output,
    prepare_runtime_grok_home,
    resolve_mcp_url,
    resolve_rimapi_url,
)
from rle_harness_grok_build.options import GrokBuildOptions

logger = logging.getLogger(__name__)

_CALL_TOOL_TIMEOUT_S = 60.0
_CALL_TOOL_PROMPT = (
    "Call the rle__get_brief tool now, then call rle__end_turn with a one-line "
    "summary. Do not search for other tools. The only MCP server is rle."
)


class DockerCommand(str, Enum):
    SMOKE = "smoke"
    CAL = "cal"


def default_definitions_dir() -> Path:
    path = scenario_loader.__file__
    if path is None:
        raise RuntimeError("rle.scenarios.loader has no __file__")
    return Path(path).resolve().parent / "definitions"


def find_scenario(query: str, *, definitions_dir: Path | None = None) -> ScenarioConfig:
    """Resolve a scenario YAML by name/stem substring (default: Crashlanded)."""
    directory = definitions_dir if definitions_dir is not None else default_definitions_dir()
    query_l = query.lower()
    matches = [
        path
        for path in sorted(directory.glob("*.yaml"))
        if query_l in path.stem.lower()
    ]
    if not matches:
        raise FileNotFoundError(f"Scenario not found: {query} (looked in {directory})")
    return load_scenario(matches[0], allow_unpinned=True)


def _auth_json_arg(value: str | None) -> Path | None:
    return Path(value) if value else None


def _prepare(
    *,
    mcp_url: str,
    grok_home: str | None,
    auth_json: str | None,
    workdir: Path,
) -> Path:
    return prepare_runtime_grok_home(
        mcp_url,
        home=Path(grok_home) if grok_home else None,
        auth_json=_auth_json_arg(auth_json),
        workdir=workdir,
    )


def _run_grok_mcp_list(binary: str, workdir: Path, timeout_s: float = 20.0) -> str:
    path = shutil.which(binary)
    if path is None:
        raise FileNotFoundError(
            f"Grok Build binary {binary!r} not found on PATH "
            "(install stock grok via https://x.ai/cli/install.sh)",
        )
    proc = subprocess.run(
        [path, "mcp", "list"],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise RuntimeError(
            f"grok mcp list failed ({proc.returncode}): {output.strip()[-800:]}",
        )
    check_mcp_list_output(output)
    return output


def _smoke_mcp_list(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    mcp_url = resolve_mcp_url(args.mcp_url)
    home = _prepare(
        mcp_url=mcp_url,
        grok_home=args.grok_home,
        auth_json=args.auth_json,
        workdir=workdir,
    )
    logger.info("isolated GROK_HOME=%s (RLE-only MCP at %s)", home, mcp_url)
    output = _run_grok_mcp_list(args.binary, workdir)
    print(output.rstrip())
    print("ok: grok mcp list shows only rle (compat MCPs disabled)")
    print("deliberately not mounted from the host:")
    for src in FORBIDDEN_HOST_BIND_SOURCES:
        print(f"  - {src}")
    return 0


async def _smoke_call_tool(args: argparse.Namespace) -> int:
    if not os.environ.get("XAI_API_KEY"):
        raise SystemExit(
            "--call-tool needs XAI_API_KEY (or skip it: default smoke is grok mcp list only)",
        )
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    mock = MockRimAPI()
    async with RimAPIClient("http://mock") as client:
        mock.attach(client)
        ledger = TickLedger(harness_name="grok-build")
        session = McpSession(
            client=client, executor=ActionExecutor(client), ledger=ledger,
        )
        manager = GameStateManager(client, expected_duration_days=30)
        state = await manager.refresh()
        session.begin_tick(0, state, build_brief(state, tick=0, macro_time=0.0))
        host = McpHost(build_server(session))
        mcp_url = await host.start()
        try:
            home = _prepare(
                mcp_url=mcp_url,
                grok_home=args.grok_home,
                auth_json=args.auth_json,
                workdir=workdir,
            )
            logger.info("isolated GROK_HOME=%s live mock MCP at %s", home, mcp_url)
            list_out = _run_grok_mcp_list(args.binary, workdir)
            print(list_out.rstrip())
            check_mcp_list_output(list_out)
            binary = shutil.which(args.binary)
            if binary is None:
                raise FileNotFoundError(f"Grok Build binary {args.binary!r} not found on PATH")
            cmd = [
                binary, "-p", _CALL_TOOL_PROMPT,
                "--output-format", "json",
                "--yolo",
                "--cwd", str(workdir),
                "--no-subagents",
                "--no-plan",
                "-m", args.model,
                "--max-turns", "6",
            ]
            logger.info("grok -p smoke (timeout %.0fs)", _CALL_TOOL_TIMEOUT_S)
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(workdir),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=_CALL_TOOL_TIMEOUT_S,
                )
            except TimeoutError as exc:
                proc.kill()
                await proc.wait()
                raise SystemExit(
                    f"grok -p timed out after {_CALL_TOOL_TIMEOUT_S:.0f}s "
                    "(stock container should call an MCP tool within ~60s)",
                ) from exc
            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            if stderr.strip():
                print(stderr.strip()[-1500:], file=sys.stderr)
            if proc.returncode != 0:
                raise SystemExit(
                    f"grok -p exited {proc.returncode}: {(stderr or stdout).strip()[-800:]}",
                )
            print(stdout.strip()[-2000:])
            print("ok: headless grok -p completed against the in-process RLE MCP")
            return 0
        finally:
            await host.stop()


def run_smoke(args: argparse.Namespace) -> int:
    if args.call_tool:
        return asyncio.run(_smoke_call_tool(args))
    return _smoke_mcp_list(args)


async def _run_cal(args: argparse.Namespace) -> int:
    rimapi_url = resolve_rimapi_url(args.rimapi_url)
    # Cal MCP is hosted in-process by HeadlessCliHarness; --mcp-url is accepted
    # only so compose/docs can pass a single flag set. Isolation home still
    # needs a placeholder URL until start_agent rewrites it.
    placeholder_mcp = resolve_mcp_url(args.mcp_url)
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    home = _prepare(
        mcp_url=placeholder_mcp,
        grok_home=args.grok_home,
        auth_json=args.auth_json,
        workdir=workdir,
    )
    logger.info(
        "cal GROK_HOME=%s RimAPI=%s (in-process MCP; host plugins not mounted)",
        home, rimapi_url,
    )
    if args.mcp_url:
        logger.info("--mcp-url is ignored for cal (harness hosts MCP on localhost)")

    scenario = find_scenario(args.scenario)
    print(f"Scenario: {scenario.name}  ticks={args.ticks}  model={args.model}")
    print(f"RimAPI: {rimapi_url}")

    try:
        await wait_for_rimapi(rimapi_url, timeout=args.wait_s)
    except TimeoutError as exc:
        raise SystemExit(
            f"{exc}\n"
            "Hint: on Docker Desktop use extra_hosts host.docker.internal:host-gateway "
            f"and RimAPI on port 8765; on an RLE compose network use "
            f"{DEFAULT_COMPOSE_RIMWORLD_URL}.",
        ) from exc

    config = RLEConfig(
        rimapi_url=rimapi_url,
        model=args.model,
        harness="grok-build",
        tick_interval=args.tick_interval,
    )
    options = GrokBuildOptions(model=args.model, max_turns=args.max_turns)
    scorer = CompositeScorer(scenario.scoring_weights or None)
    recorder = TimeSeriesRecorder()
    evaluator = ScenarioEvaluator(scenario)

    async with RimAPIClient(config.rimapi_url) as client:
        if scenario.save_name:
            try:
                await load_save_and_settle(client, config.rimapi_url, scenario.save_name)
                print(f"Loaded save: {scenario.save_name}")
            except Exception as exc:
                logger.warning("could not load save %s: %s", scenario.save_name, exc)
                print(f"Warning: could not load save {scenario.save_name!r}: {exc}")
                print("Continuing with current game state...")

        ctx = HarnessContext(
            config=config,
            client=client,
            expected_duration_days=scenario.expected_duration_days,
            initial_population=scenario.initial_population,
            scenario=scenario,
            tick_timeout_s=config.tick_timeout_s,
        )
        harness = create_harness("grok-build", ctx, options)
        loop = RLEGameLoop(
            config, client,
            expected_duration_days=scenario.expected_duration_days,
            scorer=scorer,
            recorder=recorder,
            evaluator=evaluator,
            initial_population=scenario.initial_population,
            harness=harness,
            harness_context=ctx,
            scenario=scenario,
        )
        await loop.run(max_ticks=args.ticks)

    if recorder.snapshots:
        last = recorder.snapshots[-1]
        print(f"COMPOSITE {last.composite:.3f} after {len(loop.tick_results)} tick(s)")
    else:
        print(f"No scores recorded ({len(loop.tick_results)} tick(s))")
    return 0


def run_cal(args: argparse.Namespace) -> int:
    return asyncio.run(_run_cal(args))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rle-grok-docker",
        description=(
            "Stock grok-build + RLE harness in an isolated GROK_HOME. "
            "Auth via XAI_API_KEY or a mounted auth.json — never host plugins."
        ),
    )
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    smoke = sub.add_parser(
        "smoke",
        help="Prove grok mcp list shows only rle (no RimWorld). Optional --call-tool.",
    )
    smoke.add_argument("--mcp-url", default=None, help="Written into isolated config.toml")
    smoke.add_argument(
        "--rimapi-url", default=None,
        help="Accepted for flag symmetry; smoke does not call RimAPI",
    )
    smoke.add_argument("--model", default="grok-4.6")
    smoke.add_argument("--binary", default="grok")
    smoke.add_argument("--grok-home", default=None)
    smoke.add_argument(
        "--auth-json", default=None,
        help="Optional auth.json to copy (not config.toml)",
    )
    smoke.add_argument("--workdir", default=".")
    smoke.add_argument(
        "--call-tool", action="store_true",
        help="Start a MockRimAPI MCP and run grok -p (requires XAI_API_KEY)",
    )

    cal = sub.add_parser(
        "cal",
        help="N-tick Crashlanded run against RimAPI (default 1 tick)",
    )
    cal.add_argument(
        "--rimapi-url", default=None,
        help=f"Default: RIMAPI_URL or {DEFAULT_DOCKER_RIMAPI_URL}",
    )
    cal.add_argument("--mcp-url", default=None, help="Ignored for cal (in-process MCP)")
    cal.add_argument("--model", default="grok-4.6")
    cal.add_argument("--scenario", default="crashlanded")
    cal.add_argument("--ticks", type=int, default=1)
    cal.add_argument("--tick-interval", type=float, default=1.0)
    cal.add_argument("--max-turns", type=int, default=20)
    cal.add_argument("--wait-s", type=float, default=60.0, help="RimAPI health wait")
    cal.add_argument("--binary", default="grok")
    cal.add_argument("--grok-home", default=None)
    cal.add_argument("--auth-json", default=None)
    cal.add_argument("--workdir", default=".")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    try:
        command = DockerCommand(args.command)
    except ValueError:
        print(f"unknown command: {args.command}", file=sys.stderr)
        return 2
    match command:
        case DockerCommand.SMOKE:
            return run_smoke(args)
        case DockerCommand.CAL:
            return run_cal(args)
        case _:
            assert_never(command)


if __name__ == "__main__":
    raise SystemExit(main())
