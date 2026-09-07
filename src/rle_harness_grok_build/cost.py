"""Normalize provider spend fields into the extras RLE CostSnapshot reads.

Grok's headless ``json`` object and ACP ``usage_update`` use several aliases
(``total_cost_usd``, ``cost_in_usd``, ``cost.amount``, ``costUSD``, …). RLE
looks at ``extras.cost_usd`` (and generation IDs) even when token counts are
zero, and treats ``cost_source == "billed"`` as provider-truth instead of
estimating from tokens.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
            continue
        total += parsed
        found = True
    return total if found else None


def parse_cost_usd(*sources: Any) -> float | None:
    """Return the first provider dollar amount found in *sources*.

    Incomplete bills (``cost_is_partial``) are rejected so RLE estimates
    instead of treating a partial float as billed truth.
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
    return None


def parse_generation_ids(*sources: Any) -> list[str]:
    """Collect provider generation / request IDs (order-preserving, unique).

    ``sessionId`` is not a generation ID — RLE already reads ``session_id``.
    """
    ids: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if value is None or isinstance(value, bool):
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                add(item)
            return
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            ids.append(text)

    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for key in _GENERATION_ID_KEYS:
            if key in source:
                add(source.get(key))
        nested_usage = source.get("usage")
        if isinstance(nested_usage, Mapping):
            for item in parse_generation_ids(nested_usage):
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
