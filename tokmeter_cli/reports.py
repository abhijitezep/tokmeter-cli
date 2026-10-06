"""Markdown table formatters for all seven report sections."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import pricing
from .metrics import NANO_PER_AIU, USD_PER_AIU


def _fmt_int(v: Any) -> str:
    if v is None:
        return "—"
    return f"{int(v):,}"


def _fmt_float(v: Any, decimals: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v:.{decimals}f}"


def _fmt_pct(v: Any) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.1f}%"


def _fmt_usd(v: Any) -> str:
    if v is None:
        return "—"
    return f"${v:.4f}"


def _fmt_cost_range(row: dict[str, Any]) -> str:
    """Format cost as exact, range, or estimated single figure."""
    floor = row.get("usd_floor")
    ceiling = row.get("usd_ceiling")
    is_range = row.get("usd_is_range", False)
    usd = row.get("usd")
    priced = row.get("priced_requests")
    requests = row.get("requests")

    if priced is not None and priced == requests and usd is not None:
        return _fmt_usd(usd) if usd else "—"

    if floor is None and ceiling is None:
        return _fmt_usd(usd) if usd else "—"

    if is_range:
        return f"{_fmt_usd(floor)}–{_fmt_usd(ceiling)} (est.)"

    best = floor if floor is not None else ceiling
    if best is None or best == 0:
        if usd:
            return _fmt_usd(usd)
        return "—"

    estimated = row.get("estimated_requests") or 0
    if estimated > 0:
        return f"{_fmt_usd(best)} (est.)"
    return _fmt_usd(best)


def _md_table(headers: list[str], rows: list[list[str]], align: list[str] | None = None) -> str:
    """Render a GFM table with alignment markers."""
    if not rows:
        return "_No data in this window._"

    if align is None:
        align = ["l"] * len(headers)

    sep = []
    for a in align:
        if a == "r":
            sep.append("---:")
        elif a == "c":
            sep.append(":---:")
        else:
            sep.append(":---")

    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(sep) + " |",
    ]
    for row in rows:
        cells = [str(c) for c in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def report_overview(row: dict[str, Any]) -> str:
    """Grand totals as a two-column label/value table."""
    entries: list[tuple[str, str]] = [
        ("**Volume**", ""),
        ("Requests", _fmt_int(row.get("requests"))),
        ("Sessions", _fmt_int(row.get("sessions"))),
        ("Models", _fmt_int(row.get("models"))),
        ("Total tokens", _fmt_int(row.get("total_tokens"))),
        ("Pure tokens (cache-excluded)", _fmt_int(row.get("pure_tokens"))),
        ("Input (prompt) tokens", _fmt_int(row.get("prompt_tokens"))),
        ("Fresh input tokens", _fmt_int(row.get("fresh_input_tokens"))),
        ("Cache read tokens", _fmt_int(row.get("cache_read_tokens"))),
        ("Cache write tokens", _fmt_int(row.get("cache_write_tokens"))),
        ("Output tokens", _fmt_int(row.get("output_tokens"))),
        ("Reasoning tokens", _fmt_int(row.get("reasoning_tokens"))),
        ("Avg tokens/request", _fmt_float(row.get("avg_total_tokens"))),
        ("Avg TTFT (ms)", _fmt_float(row.get("avg_ttft_ms"))),
        ("", ""),
        ("**Cost**", ""),
        ("Recorded (CLI billed)", _fmt_usd(row.get("usd"))),
        ("Best estimate (floor)", _fmt_usd(row.get("usd_floor"))),
        ("Ceiling estimate", _fmt_usd(row.get("usd_ceiling"))),
        ("Cost is range", "yes" if row.get("usd_is_range") else "no"),
        ("AI Units", _fmt_float(row.get("aiu"), 6)),
        ("Premium requests", _fmt_float(row.get("premium_requests"), 2)),
        ("", ""),
        ("**Cache**", ""),
        ("Cache hit rate", _fmt_pct(row.get("cache_hit_rate"))),
        ("Cache read tokens", _fmt_int(row.get("cache_read_tokens"))),
        ("Cache write tokens", _fmt_int(row.get("cache_write_tokens"))),
        ("", ""),
        ("**Data Quality**", ""),
        ("Token coverage", _fmt_pct(row.get("token_coverage"))),
        ("Complete requests", _fmt_int(row.get("complete_requests"))),
        ("Partial requests", _fmt_int(row.get("partial_requests"))),
        ("No-data requests", _fmt_int(row.get("nodata_requests"))),
        ("Errored requests", _fmt_int(row.get("errored_requests"))),
        ("Pending requests", _fmt_int(row.get("pending_requests"))),
        ("Priced requests (CLI)", _fmt_int(row.get("priced_requests"))),
        ("Unpriced requests", _fmt_int(row.get("unpriced_requests"))),
    ]

    rows = [[label, value] for label, value in entries]
    return _md_table(["Metric", "Value"], rows, ["l", "l"])


def report_by_model(rows: list[dict[str, Any]]) -> str:
    """Per-model breakdown."""
    headers = [
        "Model", "Requests", "Input", "Fresh", "Cache Read", "Cache Write",
        "Output", "Total", "Cache Hit", "Cost",
    ]
    align = ["l", "r", "r", "r", "r", "r", "r", "r", "r", "l"]

    table_rows = []
    for r in rows:
        table_rows.append([
            r.get("model") or "—",
            _fmt_int(r.get("requests")),
            _fmt_int(r.get("prompt_tokens")),
            _fmt_int(r.get("fresh_input_tokens")),
            _fmt_int(r.get("cache_read_tokens")),
            _fmt_int(r.get("cache_write_tokens")),
            _fmt_int(r.get("output_tokens")),
            _fmt_int(r.get("total_tokens")),
            _fmt_pct(r.get("cache_hit_rate")),
            _fmt_cost_range(r),
        ])
    return _md_table(headers, table_rows, align)


def report_sessions(rows: list[dict[str, Any]]) -> str:
    """Top sessions ranked by cost then tokens."""
    headers = [
        "#", "Session", "Summary", "Models", "Requests",
        "Total Tokens", "Cost", "Last Used",
    ]
    align = ["r", "l", "l", "l", "r", "r", "l", "l"]

    table_rows = []
    for i, r in enumerate(rows, 1):
        sid = str(r.get("session_id") or "")
        summary = str(r.get("summary") or "")
        table_rows.append([
            str(i),
            sid[:16] + ("…" if len(sid) > 16 else ""),
            summary[:40] + ("…" if len(summary) > 40 else ""),
            r.get("models") or "—",
            _fmt_int(r.get("requests")),
            _fmt_int(r.get("total_tokens")),
            _fmt_cost_range(r),
            str(r.get("last_used") or "—")[:19],
        ])
    return _md_table(headers, table_rows, align)


def report_requests(rows: list[dict[str, Any]]) -> str:
    """Recent individual requests."""
    headers = [
        "Time", "Model", "Initiator", "Input", "Fresh",
        "Cache Read", "Output", "Total", "Duration", "Cost",
    ]
    align = ["l", "l", "l", "r", "r", "r", "r", "r", "r", "l"]

    table_rows = []
    for r in rows:
        table_rows.append([
            str(r.get("created_at") or "—")[:19],
            r.get("model") or "—",
            r.get("initiator") or "—",
            _fmt_int(r.get("prompt_tokens")),
            _fmt_int(r.get("fresh_input_tokens")),
            _fmt_int(r.get("cache_read_tokens")),
            _fmt_int(r.get("output_tokens")),
            _fmt_int(r.get("total_tokens")),
            _fmt_int(r.get("duration_ms")),
            _fmt_usd(r.get("usd")),
        ])
    return _md_table(headers, table_rows, align)


def report_token_types(rows: list[dict[str, Any]]) -> str:
    """Cost breakdown by token type from the embedded rate card."""
    headers = ["Model", "Token Type", "Tokens", "Cost (USD)", "AIU"]
    align = ["l", "l", "r", "r", "r"]

    table_rows = []
    for r in rows:
        table_rows.append([
            r.get("model") or "—",
            r.get("token_type") or "—",
            _fmt_int(r.get("tokens")),
            _fmt_usd(r.get("usd")),
            _fmt_float(r.get("aiu"), 6),
        ])
    return _md_table(headers, table_rows, align)


def report_timeseries(rows: list[dict[str, Any]]) -> str:
    """Daily per-model totals."""
    headers = ["Day", "Model", "Requests", "Total Tokens", "Cache Hit", "Cost"]
    align = ["l", "l", "r", "r", "r", "l"]

    table_rows = []
    for r in rows:
        table_rows.append([
            r.get("day") or "—",
            r.get("model") or "—",
            _fmt_int(r.get("requests")),
            _fmt_int(r.get("total_tokens")),
            _fmt_pct(r.get("cache_hit_rate")),
            _fmt_cost_range(r),
        ])
    return _md_table(headers, table_rows, align)


def report_insights(data: dict[str, Any]) -> str:
    """Business metrics: run rate, cache savings, initiator breakdown."""
    parts: list[str] = []

    period = data.get("period") or {}
    spend = data.get("spend") or {}
    cache = data.get("cache") or {}
    by_init = data.get("by_initiator") or []

    # Period & run rate
    period_rows = [
        ["Active days", _fmt_int(period.get("active_days"))],
        ["First day", str(period.get("first_day") or "—")],
        ["Last day", str(period.get("last_day") or "—")],
        ["Spend (best estimate)", _fmt_usd(spend.get("usd_floor"))],
        ["Cost/active day", _fmt_usd(spend.get("per_active_day_usd"))],
        ["Projected monthly (20d)", _fmt_usd(spend.get("projected_monthly_usd"))],
    ]
    parts.append("### Run Rate\n")
    parts.append(_md_table(["Metric", "Value"], period_rows, ["l", "l"]))

    # Cache savings
    cache_rows = [
        ["Actual cost", _fmt_usd(cache.get("actual_usd"))],
        ["Baseline (no caching)", _fmt_usd(cache.get("baseline_usd"))],
        ["Saved", _fmt_usd(cache.get("saved_usd"))],
        ["Savings rate", _fmt_pct(cache.get("saved_pct"))],
        ["Cache read tokens", _fmt_int(cache.get("read_tokens"))],
        ["Cache write tokens", _fmt_int(cache.get("write_tokens"))],
    ]
    parts.append("\n\n### Cache Savings\n")
    parts.append(_md_table(["Metric", "Value"], cache_rows, ["l", "l"]))

    # By initiator
    if by_init:
        headers = ["Initiator", "Requests", "Total Tokens", "Share", "Cost"]
        align = ["l", "r", "r", "r", "l"]
        init_rows = []
        for r in by_init:
            init_rows.append([
                r.get("initiator") or "—",
                _fmt_int(r.get("requests")),
                _fmt_int(r.get("total_tokens")),
                _fmt_pct(r.get("share")),
                _fmt_cost_range(r),
            ])
        parts.append("\n\n### By Initiator\n")
        parts.append(_md_table(headers, init_rows, align))

    return "\n".join(parts)


def assemble(
    sections: list[tuple[str, str]],
    days: int | None = None,
    model: str | None = None,
    source: str | None = None,
) -> str:
    """Join all sections into one markdown document with a metadata header."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    age = pricing.age_days()

    filters = []
    if days:
        filters.append(f"last {days} day{'s' if days != 1 else ''}")
    else:
        filters.append("all time")
    if model:
        filters.append(f"model: {model}")
    if source:
        filters.append(f"source: {source}")
    filter_str = ", ".join(filters)

    lines = [
        "# Copilot Token Usage Report",
        "",
        f"Generated: {now}  |  Filter: {filter_str}",
        f"Rate table: {pricing.TRANSCRIBED} ({age} days old)",
        "",
        "---",
        "",
    ]

    for title, content in sections:
        lines.append(f"## {title}")
        lines.append("")
        lines.append(content)
        lines.append("")

    return "\n".join(lines)
