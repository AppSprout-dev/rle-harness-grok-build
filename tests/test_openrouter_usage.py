"""OpenRouter / openai_compat usage shapes → RLE CostSnapshot extras."""

from __future__ import annotations

from pathlib import Path

from rle_harness_grok_build.cost import (
    COST_SOURCE_BILLED,
    collect_metering,
    parse_cost_usd,
    parse_generation_ids,
    parse_usage_tokens,
)
from rle_harness_grok_build.harness import parse_json_output

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestOpenRouterAliases:
    def test_prompt_tokens_and_usage_cost(self) -> None:
        payload = {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "completion_tokens_details": {"reasoning_tokens": 7},
                "cost": 0.0015,
            },
            "id": "gen-abc123",
        }
        tokens = parse_usage_tokens(payload)
        assert (tokens.prompt_tokens, tokens.completion_tokens, tokens.reasoning_tokens) == (
            100, 20, 7,
        )
        assert parse_cost_usd(payload) == 0.0015
        assert parse_generation_ids(payload) == ["gen-abc123"]

    def test_ignores_non_openrouter_id(self) -> None:
        assert parse_generation_ids({"id": "call_tool_1"}) == []
        assert parse_generation_ids({"id": "chatcmpl-openai"}) == []

    def test_model_usage_camel_case(self) -> None:
        payload = {
            "modelUsage": {
                "google/gemini-3.8-flash": {
                    "inputTokens": 2100,
                    "outputTokens": 340,
                    "cacheReadInputTokens": 50,
                    "modelCalls": 4,
                    "costUSD": 0.0042,
                },
            },
        }
        tokens = parse_usage_tokens(payload)
        assert tokens.billable_prompt == 2150
        assert tokens.completion_tokens == 340
        assert parse_cost_usd(payload) == 0.0042

    def test_generation_api_lookup_shape(self) -> None:
        payload = {
            "data": {
                "id": "gen-lookup-1",
                "tokens_prompt": 2200,
                "tokens_completion": 310,
                "native_tokens_reasoning": 55,
                "total_cost": 0.0019,
            },
        }
        tokens = parse_usage_tokens(payload["data"])
        assert (tokens.prompt_tokens, tokens.completion_tokens, tokens.reasoning_tokens) == (
            2200, 310, 55,
        )
        assert parse_cost_usd(payload) == 0.0019
        assert parse_generation_ids(payload) == ["gen-lookup-1"]


class TestParseJsonOpenRouterFixtures:
    def test_headless_openai_compat_object(self) -> None:
        turn = parse_json_output(_load("openrouter_grok_headless.json"))
        assert turn.text == "Placed a bed for Lee."
        assert turn.prompt_tokens == 20820
        assert turn.completion_tokens == 612
        assert turn.reasoning_tokens == 80
        assert turn.extras["cost_usd"] == 0.00486
        assert turn.extras["cost_source"] == COST_SOURCE_BILLED
        assert turn.extras["generation_ids"] == ["gen-or-gemini-flash-001"]
        assert turn.extras["session_id"] == "sess-or-flash-1"
        assert turn.extras["usage"]["prompt_tokens"] == 18420

    def test_pretty_printed_with_log_noise(self) -> None:
        turn = parse_json_output(_load("openrouter_grok_pretty.json"))
        assert turn.text == "Called rle__end_turn."
        assert (turn.prompt_tokens, turn.completion_tokens) == (900, 40)
        assert turn.extras["cost_usd"] == 0.00021
        assert turn.extras["cost_source"] == COST_SOURCE_BILLED
        assert turn.extras["generation_id"] == "gen-pretty-1"

    def test_streaming_json_sums_per_call_usage_when_end_has_none(self) -> None:
        turn = parse_json_output(_load("openrouter_streaming.jsonl"))
        assert turn.text == "Checking the colony."
        assert (turn.prompt_tokens, turn.completion_tokens) == (1500, 100)
        assert turn.extras["cost_usd"] == 0.0004
        assert turn.extras["cost_source"] == COST_SOURCE_BILLED
        assert turn.extras["session_id"] == "sess-stream"
        assert "gen-stream-a" in turn.extras["generation_ids"]
        assert "gen-stream-b" in turn.extras["generation_ids"]

    def test_generation_lookup_document(self) -> None:
        turn = parse_json_output(_load("openrouter_generation_lookup.json"))
        assert turn.prompt_tokens == 2200
        assert turn.completion_tokens == 310
        assert turn.reasoning_tokens == 55
        assert turn.extras["cost_usd"] == 0.0019
        assert turn.extras["generation_id"] == "gen-lookup-1"

    def test_stderr_openrouter_chunk_when_stdout_has_no_usage(self) -> None:
        stdout = '{"text":"ok","sessionId":"s-1","stopReason":"end_turn"}'
        stderr = (
            '{"id":"gen-from-stderr","usage":{"prompt_tokens":50,'
            '"completion_tokens":8,"cost":0.00007}}'
        )
        turn = parse_json_output(stdout, stderr)
        assert turn.prompt_tokens == 50
        assert turn.completion_tokens == 8
        assert turn.extras["cost_usd"] == 0.00007
        assert turn.extras["generation_ids"] == ["gen-from-stderr"]
        assert turn.extras["session_id"] == "s-1"

    def test_xai_aggregate_not_double_counted_with_usage_events(self) -> None:
        stdout = "\n".join((
            '{"type":"usage","usage":{"input_tokens":10,"output_tokens":2,"cost_in_usd":0.01}}',
            '{"text":"done","sessionId":"s-xai","usage":{"input_tokens":99,"output_tokens":3},'
            '"total_cost_usd":0.08,"requestId":"req-xai"}',
        ))
        turn = parse_json_output(stdout)
        assert (turn.prompt_tokens, turn.completion_tokens) == (99, 3)
        assert turn.extras["cost_usd"] == 0.08
        assert turn.extras["generation_id"] == "req-xai"


class TestCollectMetering:
    def test_empty_text(self) -> None:
        metering = collect_metering("")
        assert metering.primary is None
        assert not metering.tokens.any_tokens()
        assert metering.cost_usd is None
        assert metering.generation_ids == []
