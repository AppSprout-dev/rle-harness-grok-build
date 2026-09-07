"""Normalize provider spend fields into the extras RLE CostSnapshot reads.

Grok's headless ``json`` object and ACP ``usage_update`` use several aliases
(``total_cost_usd``, ``cost_in_usd``, ``cost.amount``, ``costUSD``, …). OpenRouter
/ OpenAI-compat traffic uses a different wire shape (``prompt_tokens``,
``usage.cost``, ``id: gen-…``) that the XAI projector may pass through or emit
on a sibling ``usage`` / streaming-json line. RLE looks at ``extras.cost_usd``
(and generation IDs) even when token counts are zero, and treats
``cost_source == "billed"`` as provider-truth instead of estimating from tokens.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

COST_SOURCE_BILLED = "billed"

# First match wins. ``cost_usd`` is already the extras key; keep it first so a
# payload that already used the RLE name is not overwritten by a sibling alias.
_COST_USD_ALIASES: tuple[str, ...] = (
    "cost_usd",
    "total_cost_usd",
    "cost_in_usd",
    "total_cost",
    "costUSD",
)

_GENERATION_ID_KEYS: tuple[str, ...] = (
    "generation_id",
    "generationId",
    "generation_ids",
    "generationIds",
    "requestId",
    "request_id",
    "messageId",
    "message_id",
)

# OpenAI / OpenRouter chat-completion token names, plus grok headless / ACP.
_PROMPT_TOKEN_KEYS: tuple[str, ...] = (
    "input_tokens",
    "prompt_tokens",
    "inputTokens",
    "promptTokens",
    "tokens_prompt",
    "native_tokens_prompt",
)
_COMPLETION_TOKEN_KEYS: tuple[str, ...] = (
    "output_tokens",
    "completion_tokens",
    "outputTokens",
    "completionTokens",
    "tokens_completion",
    "native_tokens_completion",
)
_REASONING_TOKEN_KEYS: tuple[str, ...] = (
    "reasoning_tokens",
    "reasoningTokens",
    "native_tokens_reasoning",
)
_CACHE_READ_KEYS: tuple[str, ...] = (
    "cache_read_input_tokens",
    "cacheReadInputTokens",
    "cached_prompt_tokens",
    "cached_tokens",
    "native_tokens_cached",
)
_CACHE_CREATE_KEYS: tuple[str, ...] = (
    "cache_creation_input_tokens",
    "cacheCreationInputTokens",
    "cache_creation_prompt_tokens",
    "cache_write_tokens",
)

_STREAM_EVENT_TYPES = frozenset({
    "text",
    "thought",
    "tool_call",
    "tool_call_update",
    "usage",
    "plan",
    "available_commands",
    "end",
    "error",
})


@dataclass(frozen=True)
class UsageTokens:
    """Canonical token counts plus the raw usage mapping (if any)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    raw: dict[str, Any] | None = None

    @property
    def billable_prompt(self) -> int:
        """Uncached prompt + cache buckets (matches grok headless policy)."""
        return self.prompt_tokens + self.cached_tokens

    def any_tokens(self) -> bool:
        return bool(
            self.prompt_tokens
            or self.completion_tokens
            or self.reasoning_tokens
            or self.cached_tokens
        )


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(float(text))
        except ValueError:
            return None
    return None


def _first_int(source: Mapping[str, Any], keys: Sequence[str]) -> int:
    for key in keys:
        if key in source:
            parsed = _as_int(source.get(key))
            if parsed is not None:
                return parsed
    return 0


def _as_usd(value: Any) -> float | None:
    """Coerce a provider money field to USD, or None if it is not a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    if isinstance(value, Mapping):
        if "amount" in value:
            parsed = _as_usd(value.get("amount"))
            if parsed is not None:
                return parsed
        for key in _COST_USD_ALIASES:
            if key in value:
                parsed = _as_usd(value.get(key))
                if parsed is not None:
                    return parsed
    return None


def _model_usage_cost_usd(model_usage: Any) -> float | None:
    """Sum ``modelUsage.*.costUSD`` when the top-level total is absent."""
    if not isinstance(model_usage, Mapping):
        return None
    total = 0.0
    found = False
    for row in model_usage.values():
        if not isinstance(row, Mapping):
            continue
        parsed = _as_usd(row.get("costUSD"))
        if parsed is None:
            parsed = _as_usd(row.get("cost_usd"))
        if parsed is None:
            parsed = _as_usd(row.get("cost"))
        if parsed is None:
            continue
        total += parsed
        found = True
    return total if found else None


def _model_usage_tokens(model_usage: Any) -> UsageTokens | None:
    """Sum camelCase ``modelUsage.*`` rows when top-level ``usage`` is empty."""
    if not isinstance(model_usage, Mapping):
        return None
    prompt = completion = reasoning = cached = 0
    found = False
    raw: dict[str, Any] = {}
    for row in model_usage.values():
        if not isinstance(row, Mapping):
            continue
        part = _tokens_from_mapping(row)
        if not part.any_tokens():
            continue
        prompt += part.prompt_tokens
        completion += part.completion_tokens
        reasoning += part.reasoning_tokens
        cached += part.cached_tokens
        found = True
        raw.update(row)
    if not found:
        return None
    return UsageTokens(
        prompt_tokens=prompt,
        completion_tokens=completion,
        reasoning_tokens=reasoning,
        cached_tokens=cached,
        raw=raw,
    )


def _tokens_from_mapping(source: Mapping[str, Any]) -> UsageTokens:
    prompt = _first_int(source, _PROMPT_TOKEN_KEYS)
    completion = _first_int(source, _COMPLETION_TOKEN_KEYS)
    reasoning = _first_int(source, _REASONING_TOKEN_KEYS)
    details = source.get("completion_tokens_details")
    if reasoning == 0 and isinstance(details, Mapping):
        reasoning = _first_int(details, _REASONING_TOKEN_KEYS)
    cached = _first_int(source, _CACHE_READ_KEYS) + _first_int(source, _CACHE_CREATE_KEYS)
    prompt_details = source.get("prompt_tokens_details")
    if cached == 0 and isinstance(prompt_details, Mapping):
        cached = _first_int(prompt_details, _CACHE_READ_KEYS) + _first_int(
            prompt_details, _CACHE_CREATE_KEYS,
        )
    return UsageTokens(
        prompt_tokens=prompt,
        completion_tokens=completion,
        reasoning_tokens=reasoning,
        cached_tokens=cached,
        raw=dict(source),
    )


def parse_usage_tokens(*sources: Any) -> UsageTokens:
    """Return the first non-zero token bag found in *sources*.

    Accepts grok headless ``input_tokens``, OpenAI/OpenRouter ``prompt_tokens``,
    generation-API ``tokens_prompt``, nested ``usage``, and camelCase
    ``modelUsage`` rows.
    """
    empty = UsageTokens()
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        nested = source.get("usage")
        if isinstance(nested, Mapping):
            tokens = _tokens_from_mapping(nested)
            if tokens.any_tokens():
                return tokens
        tokens = _tokens_from_mapping(source)
        if tokens.any_tokens():
            return tokens
        model_tokens = _model_usage_tokens(source.get("modelUsage"))
        if model_tokens is not None and model_tokens.any_tokens():
            return model_tokens
        nested_data = source.get("data")
        if isinstance(nested_data, Mapping):
            tokens = parse_usage_tokens(nested_data)
            if tokens.any_tokens():
                return tokens
    return empty


def parse_cost_usd(*sources: Any) -> float | None:
    """Return the first provider dollar amount found in *sources*.

    Incomplete bills (``cost_is_partial``) are rejected so RLE estimates
    instead of treating a partial float as billed truth. OpenRouter native
    ``usage.cost`` (USD float) is accepted via the ``cost`` key.
    """
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        if source.get("cost_is_partial") is True:
            continue
        for key in _COST_USD_ALIASES:
            if key in source:
                parsed = _as_usd(source.get(key))
                if parsed is not None:
                    return parsed
        if "cost" in source:
            parsed = _as_usd(source.get("cost"))
            if parsed is not None:
                return parsed
        nested_usage = source.get("usage")
        if isinstance(nested_usage, Mapping):
            parsed = parse_cost_usd(nested_usage)
            if parsed is not None:
                return parsed
        parsed = _model_usage_cost_usd(source.get("modelUsage"))
        if parsed is not None:
            return parsed
        nested_data = source.get("data")
        if isinstance(nested_data, Mapping):
            parsed = parse_cost_usd(nested_data)
            if parsed is not None:
                return parsed
    return None


def _is_openrouter_generation_id(text: str) -> bool:
    """OpenRouter chat-completion ``id`` used by ``GET /api/v1/generation``."""
    return text.startswith("gen-")


def parse_generation_ids(*sources: Any) -> list[str]:
    """Collect provider generation / request IDs (order-preserving, unique).

    ``sessionId`` is not a generation ID — RLE already reads ``session_id``.
    A bare ``id`` is kept only when it is an OpenRouter ``gen-…`` id so tool
    call ids and chunk ids do not pollute billed reconciliation.
    """
    ids: list[str] = []
    seen: set[str] = set()

    def add(value: Any, *, require_or_prefix: bool = False) -> None:
        if value is None or isinstance(value, bool):
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                add(item, require_or_prefix=require_or_prefix)
            return
        text = str(value).strip()
        if not text or text in seen:
            return
        if require_or_prefix and not _is_openrouter_generation_id(text):
            return
        seen.add(text)
        ids.append(text)

    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for key in _GENERATION_ID_KEYS:
            if key in source:
                add(source.get(key))
        if "id" in source:
            add(source.get("id"), require_or_prefix=True)
        nested_usage = source.get("usage")
        if isinstance(nested_usage, Mapping):
            for item in parse_generation_ids(nested_usage):
                add(item)
        nested_data = source.get("data")
        if isinstance(nested_data, Mapping):
            for item in parse_generation_ids(nested_data):
                add(item)
    return ids


def provider_cost_extras(
    *,
    cost_usd: float | None = None,
    generation_ids: Sequence[str] = (),
    usage: Any = None,
    cost: Any = None,
) -> dict[str, Any]:
    """Build the extras bag RLE reads for billed vs estimated spend.

    ``cost_source`` is ``billed`` only when a money/amount is present.
    Generation IDs and ``usage`` are included even when tokens are zero.
    """
    extras: dict[str, Any] = {}
    if cost_usd is not None:
        extras["cost_usd"] = cost_usd
        extras["cost_source"] = COST_SOURCE_BILLED
    if generation_ids:
        extras["generation_ids"] = list(generation_ids)
        extras["generation_id"] = generation_ids[0]
    if usage is not None:
        extras["usage"] = usage
    if cost is not None:
        extras["cost"] = cost
    return extras


def iter_json_objects(*texts: str) -> list[dict[str, Any]]:
    """Extract JSON objects from grok stdout/stderr (line, blob, pretty).

    Headless ``--output-format json`` is usually one object. OpenRouter /
    openai_compat runs may pretty-print, emit streaming-json NDJSON, or mix
    log lines with a trailing object. Last-line-only parsing misses those.
    """
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if not isinstance(value, dict):
            return
        key = json.dumps(value, sort_keys=True, default=str)
        if key in seen:
            return
        seen.add(key)
        found.append(value)

    for text in texts:
        if not text or not text.strip():
            continue
        stripped = text.strip()
        try:
            add(json.loads(stripped))
        except json.JSONDecodeError:
            pass
        for line in stripped.splitlines():
            line = line.strip()
            if not line.startswith("{") and not line.startswith("["):
                continue
            try:
                loaded = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(loaded, list):
                for item in loaded:
                    add(item)
            else:
                add(loaded)
        for obj in _extract_balanced_objects(stripped):
            add(obj)
    return found


def _extract_balanced_objects(text: str) -> list[dict[str, Any]]:
    """Pull top-level ``{…}`` objects out of pretty-printed / noisy text."""
    objects: list[dict[str, Any]] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        escape = False
        end = None
        for j in range(i, n):
            ch = text[j]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end is None:
            break
        snippet = text[i : end + 1]
        try:
            loaded = json.loads(snippet)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(loaded, dict):
            objects.append(loaded)
        i = end + 1
    return objects


def _is_stream_event(obj: Mapping[str, Any]) -> bool:
    kind = obj.get("type")
    return isinstance(kind, str) and kind in _STREAM_EVENT_TYPES


def _is_headless_result(obj: Mapping[str, Any]) -> bool:
    """True for the grok ``json`` object or streaming-json ``end``."""
    kind = obj.get("type")
    if kind == "end":
        return True
    if _is_stream_event(obj):
        return False
    return any(key in obj for key in ("sessionId", "stopReason", "text", "total_cost_usd"))


def _is_error_object(obj: Mapping[str, Any]) -> bool:
    return obj.get("type") == "error"


def select_primary_payload(objects: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Last headless result, else last non-event object, else last object."""
    for obj in reversed(objects):
        if _is_headless_result(obj) and not _is_error_object(obj):
            return dict(obj)
    for obj in reversed(objects):
        if not _is_stream_event(obj) and not _is_error_object(obj):
            return dict(obj)
    if objects:
        return dict(objects[-1])
    return None


def _result_text(primary: Mapping[str, Any] | None, objects: Sequence[Mapping[str, Any]]) -> str:
    if primary:
        if primary.get("type") == "end":
            result = primary.get("result")
            if result:
                return str(result)
        text = primary.get("text")
        if text:
            return str(text)
    parts = [
        str(obj.get("data"))
        for obj in objects
        if obj.get("type") == "text" and obj.get("data")
    ]
    if parts:
        return "".join(parts)
    if primary:
        return str(primary.get("text", "") or "")
    return ""


def _usage_mapping(*sources: Any) -> dict[str, Any] | None:
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        nested = source.get("usage")
        if isinstance(nested, Mapping) and nested:
            return dict(nested)
        tokens = _tokens_from_mapping(source)
        if tokens.any_tokens():
            return dict(source)
        model_tokens = _model_usage_tokens(source.get("modelUsage"))
        if model_tokens is not None and model_tokens.raw:
            return model_tokens.raw
        nested_data = source.get("data")
        if isinstance(nested_data, Mapping):
            found = _usage_mapping(nested_data)
            if found is not None:
                return found
    return None


def _sum_usage_tokens(objects: Iterable[Mapping[str, Any]]) -> UsageTokens:
    prompt = completion = reasoning = cached = 0
    raw: dict[str, Any] | None = None
    for obj in objects:
        tokens = parse_usage_tokens(obj)
        if not tokens.any_tokens():
            continue
        prompt += tokens.prompt_tokens
        completion += tokens.completion_tokens
        reasoning += tokens.reasoning_tokens
        cached += tokens.cached_tokens
        raw = tokens.raw
    return UsageTokens(
        prompt_tokens=prompt,
        completion_tokens=completion,
        reasoning_tokens=reasoning,
        cached_tokens=cached,
        raw=raw,
    )


def _sum_cost_usd(objects: Iterable[Mapping[str, Any]]) -> float | None:
    total = 0.0
    found = False
    for obj in objects:
        parsed = parse_cost_usd(obj)
        if parsed is None:
            continue
        total += parsed
        found = True
    return round(total, 6) if found else None


@dataclass
class CollectedMetering:
    """Tokens / billed USD / generation IDs recovered from grok stdout."""

    tokens: UsageTokens = field(default_factory=UsageTokens)
    cost_usd: float | None = None
    generation_ids: list[str] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    primary: dict[str, Any] | None = None
    text: str = ""


def collect_metering(*texts: str) -> CollectedMetering:
    """Parse grok stdout/stderr into the fields RLE CostSnapshot needs.

    Prefers a headless aggregate (``json`` object / ``end`` event) so per-call
    streaming-json ``usage`` lines are not double-counted. When that aggregate
    has no tokens and no USD, sums OpenRouter / OpenAI-shaped siblings
    (``prompt_tokens``, ``usage.cost``, ``id: gen-…``).
    """
    objects = iter_json_objects(*texts)
    primary = select_primary_payload(objects)
    generation_ids = parse_generation_ids(primary, *objects)
    tokens = parse_usage_tokens(primary) if primary is not None else UsageTokens()
    cost_usd = parse_cost_usd(primary) if primary is not None else None
    if not tokens.any_tokens() and cost_usd is None:
        fallback = [
            obj
            for obj in objects
            if obj is not primary
            and (
                obj.get("type") == "usage"
                or parse_usage_tokens(obj).any_tokens()
                or parse_cost_usd(obj) is not None
            )
        ]
        if fallback:
            tokens = _sum_usage_tokens(fallback)
            cost_usd = _sum_cost_usd(fallback)
    usage = _usage_mapping(primary, *objects) if tokens.any_tokens() or cost_usd is not None else (
        _usage_mapping(primary) if primary is not None else None
    )
    if usage is None and tokens.raw:
        usage = tokens.raw
    return CollectedMetering(
        tokens=tokens,
        cost_usd=cost_usd,
        generation_ids=generation_ids,
        usage=usage,
        primary=primary,
        text=_result_text(primary, objects),
    )
