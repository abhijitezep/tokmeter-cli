"""Published per-token rates for cost estimation of unpriced requests.

Copilot CLI rows carry their own rate card (exact cost). VS Code chat rows
have token counts only, so cost is bounded using the published table:

    https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing

Floor treats every prompt token as a cache hit; ceiling treats every one as
a cache write (Anthropic) or fresh input (others). The true cost lies between.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

TRANSCRIBED = "2026-09-14"
SOURCE_URL = "https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing"
STALE_AFTER_DAYS = 90
CACHE_WRITE_MULTIPLIER = 1.25

# model -> (input, cached input, output) in USD per 1M tokens.
PUBLISHED: dict[str, tuple[float, float, float]] = {
    "claude-haiku-4.5": (1.00, 0.10, 5.00),
    "claude-sonnet-4": (3.00, 0.30, 15.00),
    "claude-sonnet-4.6": (3.00, 0.30, 15.00),
    "claude-sonnet-5": (2.00, 0.20, 10.00),
    "claude-opus-4.7": (5.00, 0.50, 25.00),
    "claude-opus-4.8": (5.00, 0.50, 25.00),
    "claude-opus-5": (5.00, 0.50, 25.00),
    "claude-fable-5": (10.00, 1.00, 50.00),
    "gpt-5-mini": (0.25, 0.025, 2.00),
    "gpt-5.3-codex": (1.75, 0.175, 14.00),
    "gpt-5.4": (2.50, 0.25, 15.00),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5.5": (5.00, 0.50, 30.00),
    "gpt-5.6-luna": (0.20, 0.02, 1.20),
    "gpt-5.6-sol": (4.00, 0.40, 20.00),
    "gpt-5.6-terra": (2.00, 0.20, 12.00),
    "gpt-6-astra": (10.00, 1.00, 50.00),
    "gemini-3.5-flash": (1.50, 0.15, 9.00),
    "gemini-3.6-flash": (0.75, 0.075, 3.75),
    "gemini-3.7-flash": (0.75, 0.075, 3.75),
    "gemini-3.8-flash": (0.75, 0.075, 3.75),
    "grok-4.5": (2.00, 0.50, 6.00),
    "grok-4.6": (2.00, 0.50, 6.00),
    "kimi-k2.7-code": (0.95, 0.19, 4.00),
    "kimi-k3": (3.00, 0.30, 15.00),
    "mai-code-1.1-flash": (0.20, 0.02, 1.20),
}

INFERRED: dict[str, tuple[float, float, float]] = {
    "claude-opus-4.5": (5.00, 0.50, 25.00),
    "claude-opus-4.6": (5.00, 0.50, 25.00),
    "claude-sonnet-4.5": (3.00, 0.30, 15.00),
}

RATES = {**PUBLISHED, **INFERRED}

_NANO_PER_CREDIT = 1_000_000_000
_USD_PER_CREDIT = 0.01


def parse_rate_card(details: str | None) -> dict[str, float]:
    """Read a stored token_details_json card into USD per 1M tokens."""
    if not details:
        return {}
    try:
        entries = json.loads(details)
    except (TypeError, ValueError):
        return {}
    if not isinstance(entries, list):
        return {}
    card: dict[str, float] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("tokenType")
        per_batch = entry.get("costPerBatch")
        batch = entry.get("batchSize")
        if not kind or not isinstance(per_batch, (int, float)) or not batch:
            continue
        per_million = per_batch / batch * 1_000_000
        card[kind] = per_million / _NANO_PER_CREDIT * _USD_PER_CREDIT
    return card


def cache_write_rate(model: str) -> float | None:
    """Per-million rate for writing prompt tokens into the cache, if charged."""
    rates = RATES.get(model)
    if rates is None or not model.startswith("claude-"):
        return None
    return round(rates[0] * CACHE_WRITE_MULTIPLIER, 6)


def ceiling_input_rate(model: str) -> float | None:
    """Dearest a prompt token can be: cache write for Anthropic, else input."""
    rates = RATES.get(model)
    if rates is None:
        return None
    return max(rates[0], cache_write_rate(model) or 0.0)


def rate_rows() -> list[tuple[str, float, float, float, float, float, int]]:
    """Rate table as rows for SQL insertion into model_rates."""
    return [
        (
            model,
            rates[0],
            rates[1],
            rates[2],
            ceiling_input_rate(model) or rates[0],
            cache_write_rate(model) or rates[0],
            int(model in INFERRED),
        )
        for model, rates in RATES.items()
    ]


def is_priceable(model: str | None) -> bool:
    return bool(model) and model in RATES


def age_days(transcribed: str = TRANSCRIBED, today: date | None = None) -> int:
    stamped = date.fromisoformat(transcribed)
    return max((today or date.today()) - stamped, timedelta(0)).days
