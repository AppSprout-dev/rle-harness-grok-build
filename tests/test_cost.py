"""Provider-truth cost alias parsing for RLE extras.cost_usd."""

from __future__ import annotations

import json

from rle_harness_grok_build.acp import _TurnAccumulator
from rle_harness_grok_build.cost import (
    COST_SOURCE_BILLED,
    parse_cost_usd,
    parse_generation_ids,
    parse_usage_tokens,
    provider_cost_extras,
)
from rle_harness_grok_build.harness import parse_json_output


class TestParseCostUsdAliases:
    def test_total_cost_usd(self) -> None:
        assert parse_cost_usd({"total_cost_usd": 0.0127}) == 0.0127

    def test_cost_in_usd_string(self) -> None:
        assert parse_cost_usd({"cost_in_usd": "0.0042"}) == 0.0042

    def test_cost_usd_preferred_over_sibling_alias(self) -> None:
        assert parse_cost_usd({"cost_usd": 1.5, "total_cost_usd": 9.9}) == 1.5

    def test_cost_amount_object(self) -> None:
        assert parse_cost_usd({"cost": {"amount": 0.01, "currency": "USD"}}) == 0.01

    def test_nested_usage_cost_in_usd(self) -> None:
        assert parse_cost_usd({"usage": {"cost_in_usd": 0.3}}) == 0.3

    def test_openrouter_usage_cost_float(self) -> None:
        assert parse_cost_usd({"usage": {"prompt_tokens": 10, "cost": 0.0015}}) == 0.0015

    def test_model_usage_cost_usd_sum(self) -> None:
        assert parse_cost_usd({
            "modelUsage": {
                "grok-4.6": {"costUSD": 0.01},
                "grok-code": {"costUSD": 0.002},
            },
        }) == 0.012

    def test_zero_is_present_money(self) -> None:
        assert parse_cost_usd({"total_cost_usd": 0.0}) == 0.0

    def test_partial_cost_is_rejected(self) -> None:
        assert parse_cost_usd({
            "cost_is_partial": True,
            "total_cost_usd": 0.5,
        }) is None

    def test_missing_returns_none(self) -> None:
        assert parse_cost_usd({"usage": {"input_tokens": 10}}) is None
        assert parse_cost_usd("not a mapping") is None


class TestParseGenerationIds:
    def test_request_id_and_generation_id(self) -> None:
        assert parse_generation_ids({
            "requestId": "xyz789",
            "generation_id": "gen-1",
        }) == ["gen-1", "xyz789"]

    def test_nested_usage_message_id(self) -> None:
        assert parse_generation_ids({
            "usage": {"messageId": "resp_1"},
        }) == ["resp_1"]

    def test_ignores_session_id(self) -> None:
        assert parse_generation_ids({"sessionId": "abc123"}) == []

    def test_openrouter_gen_prefix_id(self) -> None:
        assert parse_generation_ids({"id": "gen-or-1"}) == ["gen-or-1"]
        assert parse_generation_ids({"id": "tool-call-9"}) == []


class TestParseUsageTokens:
    def test_xai_input_tokens_unchanged(self) -> None:
        tokens = parse_usage_tokens({
            "usage": {"input_tokens": 12, "output_tokens": 3, "reasoning_tokens": 1},
        })
        assert (tokens.prompt_tokens, tokens.completion_tokens, tokens.reasoning_tokens) == (
            12, 3, 1,
        )

    def test_openrouter_prompt_tokens(self) -> None:
        tokens = parse_usage_tokens({
            "usage": {
                "prompt_tokens": 40,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 10},
            },
        })
        assert tokens.prompt_tokens == 40
        assert tokens.cached_tokens == 10
        assert tokens.billable_prompt == 50


class TestProviderCostExtras:
    def test_billed_when_amount_present(self) -> None:
        extras = provider_cost_extras(cost_usd=0.01, generation_ids=["g1"])
        assert extras["cost_usd"] == 0.01
        assert extras["cost_source"] == COST_SOURCE_BILLED
        assert extras["generation_id"] == "g1"
        assert extras["generation_ids"] == ["g1"]

    def test_unset_source_without_money(self) -> None:
        extras = provider_cost_extras(generation_ids=["g1"], usage={"used": 0})
        assert "cost_usd" not in extras
        assert "cost_source" not in extras
        assert extras["generation_ids"] == ["g1"]
        assert extras["usage"] == {"used": 0}


class TestParseJsonOutputCostPath:
    def test_aliases_and_zero_tokens_still_bill(self) -> None:
        payload = {
            "text": "",
            "sessionId": "s-0",
            "requestId": "req-0",
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
            },
            "cost_in_usd": 0.08,
        }
        turn = parse_json_output(json.dumps(payload))
        assert (turn.prompt_tokens, turn.completion_tokens) == (0, 0)
        assert turn.extras["cost_usd"] == 0.08
        assert turn.extras["cost_source"] == "billed"
        assert turn.extras["generation_id"] == "req-0"
        assert turn.extras["usage"]["input_tokens"] == 0


class TestAcpUsageExtras:
    def test_keeps_full_usage_and_cost_amount_when_tokens_zero(self) -> None:
        acc = _TurnAccumulator()
        acc.on_update({
            "update": {
                "sessionUpdate": "usage_update",
                "used": 0,
                "size": 100000,
                "cost": {"amount": 0.42, "currency": "USD"},
                "generation_id": "gen-acp-0",
            },
        })
        turn = acc.to_turn_result(stop_reason="end_turn", session_id="sess")
        assert turn.prompt_tokens == 0
        assert turn.extras["cost_usd"] == 0.42
        assert turn.extras["cost_source"] == "billed"
        assert turn.extras["cost"] == {"amount": 0.42, "currency": "USD"}
        assert turn.extras["usage"]["used"] == 0
        assert turn.extras["usage"]["cost"]["amount"] == 0.42
        assert turn.extras["generation_id"] == "gen-acp-0"
        assert turn.extras["acp"] is True

    def test_no_cost_leaves_source_unset(self) -> None:
        acc = _TurnAccumulator()
        acc.on_update({
            "update": {
                "sessionUpdate": "usage_update",
                "used": 8,
                "size": 100000,
            },
        })
        turn = acc.to_turn_result(stop_reason="end_turn", session_id="sess")
        assert turn.prompt_tokens == 8
        assert turn.extras["usage"]["used"] == 8
        assert "cost_usd" not in turn.extras
        assert "cost_source" not in turn.extras
        assert "cost" not in turn.extras
