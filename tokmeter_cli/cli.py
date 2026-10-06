"""CLI orchestration: parse flags, run queries, assemble markdown output."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import __version__, reports
from .db import NANO_PER_AIU, USD_PER_AIU, SessionStore, SessionStoreError
from .metrics import (
    FRESH_INPUT,
    LOCAL_DAY,
    METRIC_COLUMNS,
    build_filter,
    decorate,
    session_sql,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="tokmeter",
        description="Extract Copilot token usage metrics as markdown tables.",
    )
    p.add_argument("--days", type=int, default=None,
                   help="Calendar-day window (1 = today). Omit for all-time.")
    p.add_argument("--model", type=str, default=None,
                   help="Filter to one model (exact match).")
    p.add_argument("--source", type=str, default=None,
                   choices=["copilot-cli", "vscode-chat"],
                   help="Restrict to one data source.")
    p.add_argument("--output", type=str, default=None,
                   help="Write markdown to file instead of stdout.")
    p.add_argument("--limit-sessions", type=int, default=25,
                   help="Top Sessions cap (default 25).")
    p.add_argument("--limit-requests", type=int, default=50,
                   help="Recent Requests cap (default 50).")
    p.add_argument("--no-vscode", action="store_true",
                   help="Skip VS Code journal scanning.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


def _build_token_types(
    store: SessionStore, where: str, params: list[Any]
) -> list[dict[str, Any]]:
    """Aggregate cost by token type from embedded rate cards."""
    tt_where = f"{where} AND u.token_details_json IS NOT NULL" if where else " WHERE u.token_details_json IS NOT NULL"
    rows = store.query(
        f"SELECT u.model, u.token_details_json FROM assistant_usage_events u{tt_where}",
        params,
    )
    agg: dict[tuple[str, str], dict[str, float]] = defaultdict(
        lambda: {"tokens": 0, "nano_aiu": 0.0}
    )
    for row in rows:
        try:
            details = json.loads(row["token_details_json"])
        except (TypeError, ValueError):
            continue
        for item in details:
            token_type = item.get("tokenType", "unknown")
            count = item.get("tokenCount") or 0
            batch = item.get("batchSize") or 0
            cost_per_batch = item.get("costPerBatch") or 0
            bucket = agg[(row["model"], token_type)]
            bucket["tokens"] += count
            if batch:
                bucket["nano_aiu"] += count / batch * cost_per_batch

    out = []
    for (model_name, token_type), values in agg.items():
        aiu = values["nano_aiu"] / NANO_PER_AIU
        out.append({
            "model": model_name,
            "token_type": token_type,
            "tokens": int(values["tokens"]),
            "aiu": round(aiu, 6),
            "usd": round(aiu * USD_PER_AIU, 6),
        })
    out.sort(key=lambda r: r["usd"], reverse=True)
    return out


def _build_insights(
    store: SessionStore,
    where: str,
    params: list[Any],
    spend: dict[str, Any],
    days: int | None,
) -> dict[str, Any]:
    """Business metrics: run rate, cache savings, initiator breakdown."""
    baseline_row = store.query(
        f"SELECT COALESCE(SUM(CASE"
        f" WHEN u.input_usd IS NULL THEN 0"
        f" WHEN COALESCE(u.data_status,'complete') <> 'complete' THEN 0"
        f" ELSE COALESCE(u.input_tokens,0) / 1e6 * u.input_usd"
        f"    + COALESCE(u.output_tokens,0) / 1e6 * u.output_usd END), 0) AS baseline_usd"
        f" FROM assistant_usage_events u{where}",
        params,
    )[0]
    actual_usd = spend.get("usd_floor", 0) or 0
    baseline_usd = baseline_row["baseline_usd"] or 0.0
    saved_usd = baseline_usd - actual_usd

    period = store.query(
        f"SELECT COUNT(DISTINCT {LOCAL_DAY}) AS active_days,"
        f" MIN({LOCAL_DAY}) AS first_day, MAX({LOCAL_DAY}) AS last_day"
        f" FROM assistant_usage_events u{where}",
        params,
    )[0]
    active_days = period["active_days"] or 0
    per_active_day = actual_usd / active_days if active_days else 0.0

    by_initiator = [
        decorate(r)
        for r in store.query(
            f"SELECT COALESCE(u.initiator, 'unknown') AS initiator, {METRIC_COLUMNS}"
            f" FROM assistant_usage_events u{where} GROUP BY 1",
            params,
        )
    ]
    total_usd = sum(r["usd_floor"] for r in by_initiator) or 1
    for row in by_initiator:
        row["share"] = round(row["usd_floor"] / total_usd, 4)
    by_initiator.sort(key=lambda r: r["usd_floor"], reverse=True)

    return {
        "period": {
            "days": days,
            "active_days": active_days,
            "first_day": period["first_day"],
            "last_day": period["last_day"],
        },
        "spend": {
            "usd": spend.get("usd", 0),
            "usd_floor": spend.get("usd_floor", 0),
            "usd_ceiling": spend.get("usd_ceiling", 0),
            "usd_is_range": spend.get("usd_is_range", False),
            "per_active_day_usd": round(per_active_day, 4),
            "projected_monthly_usd": round(per_active_day * 20, 2),
        },
        "cache": {
            "actual_usd": round(actual_usd, 4),
            "baseline_usd": round(baseline_usd, 4),
            "saved_usd": round(saved_usd, 4),
            "saved_pct": round(saved_usd / baseline_usd, 4) if baseline_usd else 0.0,
            "read_tokens": spend.get("cache_read_tokens", 0),
            "write_tokens": spend.get("cache_write_tokens", 0),
        },
        "by_initiator": by_initiator,
    }


def run(args: argparse.Namespace) -> str:
    """Execute all queries and return the assembled markdown."""
    store = SessionStore(skip_vscode=args.no_vscode)
    where, params = build_filter(days=args.days, model=args.model, source=args.source)

    # 1. Overview
    overview_rows = store.query(
        f"SELECT {METRIC_COLUMNS}, COUNT(DISTINCT u.session_id) AS sessions"
        f" FROM assistant_usage_events u{where}",
        params,
    )
    overview_row = decorate(overview_rows[0])
    overview_row["models"] = len(
        store.query(f"SELECT DISTINCT u.model FROM assistant_usage_events u{where}", params)
    )

    # 2. By Model
    by_model_rows = store.query(
        f"SELECT u.model, {METRIC_COLUMNS} FROM assistant_usage_events u{where}"
        " GROUP BY u.model ORDER BY 1",
        params,
    )
    by_model_rows = [decorate(r) for r in by_model_rows]
    by_model_rows.sort(key=lambda r: r["total_tokens"], reverse=True)

    # 3. Top Sessions
    session_rows = store.query(
        session_sql(where, include_empty=False, limit=args.limit_sessions),
        [*params, *params, args.limit_sessions],
    )
    session_rows = [decorate(r) for r in session_rows]

    # 4. Recent Requests
    request_rows = store.query(
        "SELECT u.id, u.created_at, u.session_id, u.model, u.turn_index, u.initiator, u.source,"
        " u.reasoning_effort, u.finish_reason, u.duration_ms, u.time_to_first_token_ms,"
        " COALESCE(u.input_tokens,0) AS prompt_tokens,"
        f" {FRESH_INPUT} AS fresh_input_tokens,"
        " COALESCE(u.cache_read_tokens,0) AS cache_read_tokens,"
        " COALESCE(u.cache_write_tokens,0) AS cache_write_tokens,"
        " COALESCE(u.output_tokens,0) AS output_tokens,"
        " COALESCE(u.reasoning_tokens,0) AS reasoning_tokens,"
        " COALESCE(u.input_tokens,0) + COALESCE(u.output_tokens,0) AS total_tokens,"
        " COALESCE(u.total_nano_aiu,0) AS total_nano_aiu"
        f" FROM assistant_usage_events u{where}"
        " ORDER BY u.id DESC LIMIT ?",
        [*params, args.limit_requests],
    )
    for row in request_rows:
        aiu = row.pop("total_nano_aiu", 0) / NANO_PER_AIU
        row["aiu"] = round(aiu, 6)
        row["usd"] = round(aiu * USD_PER_AIU, 6)

    # 5. Token Types
    token_type_rows = _build_token_types(store, where, params)

    # 6. Timeseries
    timeseries_rows = store.query(
        f"SELECT {LOCAL_DAY} AS day, u.model, {METRIC_COLUMNS}"
        f" FROM assistant_usage_events u{where}"
        " GROUP BY day, u.model ORDER BY day",
        params,
    )
    timeseries_rows = [decorate(r) for r in timeseries_rows]

    # 7. Insights
    insights_data = _build_insights(store, where, params, overview_row, args.days)

    sections = [
        ("Overview", reports.report_overview(overview_row)),
        ("By Model", reports.report_by_model(by_model_rows)),
        ("Top Sessions", reports.report_sessions(session_rows)),
        ("Recent Requests", reports.report_requests(request_rows)),
        ("Token Types", reports.report_token_types(token_type_rows)),
        ("Daily Timeseries", reports.report_timeseries(timeseries_rows)),
        ("Insights", reports.report_insights(insights_data)),
    ]

    return reports.assemble(
        sections, days=args.days, model=args.model, source=args.source
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        output = run(args)
    except SessionStoreError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.output:
        path = Path(args.output)
        if path.suffix != ".md":
            path = path.with_suffix(".md")
        path.write_text(output, encoding="utf-8")
        print(f"Report written to {path}", file=sys.stderr)
    else:
        print(output)
    return 0
