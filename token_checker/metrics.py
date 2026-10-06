"""SQL metric constants and the decorate() post-processor.

METRIC_COLUMNS is a single SQL fragment plugged into every aggregate query.
decorate() adds derived fields (cost, averages, coverage) after SQL returns.
"""

from __future__ import annotations

from typing import Any

USD_PER_AIU = 0.01
NANO_PER_AIU = 1_000_000_000
SQL_NANO_PER_AIU = float(NANO_PER_AIU)

CLI_SOURCE = "copilot-cli"

FRESH_INPUT = (
    "MAX(COALESCE(u.input_tokens,0)"
    " - COALESCE(u.cache_read_tokens,0)"
    " - COALESCE(u.cache_write_tokens,0), 0)"
)

LOCAL_DAY = "date(u.created_at, 'localtime')"
LOCAL_TODAY = "date('now', 'localtime')"

METRIC_COLUMNS = f"""
    COUNT(*) AS requests,
    COALESCE(SUM(COALESCE(u.input_tokens,0)), 0) AS prompt_tokens,
    COALESCE(SUM({FRESH_INPUT}), 0) AS fresh_input_tokens,
    COALESCE(SUM(COALESCE(u.cache_read_tokens,0)), 0) AS cache_read_tokens,
    COALESCE(SUM(COALESCE(u.cache_write_tokens,0)), 0) AS cache_write_tokens,
    COALESCE(SUM(COALESCE(u.output_tokens,0)), 0) AS output_tokens,
    COALESCE(SUM(COALESCE(u.reasoning_tokens,0)), 0) AS reasoning_tokens,
    COALESCE(SUM(COALESCE(u.input_tokens,0) + COALESCE(u.output_tokens,0)), 0) AS total_tokens,
    COALESCE(SUM(CASE WHEN u.source = '{CLI_SOURCE}' OR u.cache_read_tokens IS NOT NULL
        THEN {FRESH_INPUT} + COALESCE(u.output_tokens,0) ELSE 0 END), 0) AS pure_tokens,
    COALESCE(SUM(COALESCE(u.total_nano_aiu,0)), 0) AS total_nano_aiu,
    COALESCE(SUM(COALESCE(u.request_multiplier,0)), 0) AS premium_requests,
    COALESCE(SUM(COALESCE(u.duration_ms,0)), 0) AS duration_ms,
    AVG(u.time_to_first_token_ms) AS avg_ttft_ms,
    COUNT(DISTINCT u.source) AS source_count,
    SUM(CASE WHEN u.source = '{CLI_SOURCE}' OR u.cache_read_tokens IS NOT NULL
        THEN 1 ELSE 0 END) AS cache_detail_requests,
    SUM(CASE WHEN u.source = '{CLI_SOURCE}' THEN 1 ELSE 0 END) AS priced_requests,
    COALESCE(SUM(CASE WHEN u.source = '{CLI_SOURCE}'
        THEN COALESCE(u.input_tokens,0) + COALESCE(u.output_tokens,0) ELSE 0 END), 0) AS priced_tokens,
    COALESCE(SUM(CASE
        WHEN u.source = '{CLI_SOURCE}' THEN COALESCE(u.total_nano_aiu,0) / {SQL_NANO_PER_AIU} * {USD_PER_AIU}
        WHEN u.input_usd IS NULL THEN 0
        WHEN COALESCE(u.data_status,'complete') <> 'complete' THEN 0
        WHEN u.cache_read_tokens IS NOT NULL THEN
            {FRESH_INPUT} / 1e6 * u.input_usd
            + COALESCE(u.cache_read_tokens,0) / 1e6 * u.cached_usd
            + COALESCE(u.cache_write_tokens,0) / 1e6 * u.cache_write_usd
            + COALESCE(u.output_tokens,0) / 1e6 * u.output_usd
        ELSE COALESCE(u.input_tokens,0) / 1e6 * u.cached_usd
           + COALESCE(u.output_tokens,0) / 1e6 * u.output_usd END), 0) AS usd_floor,
    COALESCE(SUM(CASE
        WHEN u.source = '{CLI_SOURCE}' THEN COALESCE(u.total_nano_aiu,0) / {SQL_NANO_PER_AIU} * {USD_PER_AIU}
        WHEN u.input_usd IS NULL THEN 0
        WHEN COALESCE(u.data_status,'complete') <> 'complete' THEN 0
        WHEN u.cache_read_tokens IS NOT NULL THEN
            {FRESH_INPUT} / 1e6 * u.input_usd
            + COALESCE(u.cache_read_tokens,0) / 1e6 * u.cached_usd
            + COALESCE(u.cache_write_tokens,0) / 1e6 * u.cache_write_usd
            + COALESCE(u.output_tokens,0) / 1e6 * u.output_usd
        ELSE COALESCE(u.input_tokens,0) / 1e6 * u.ceiling_input_usd
           + COALESCE(u.output_tokens,0) / 1e6 * u.output_usd END), 0) AS usd_ceiling,
    SUM(CASE WHEN u.source <> '{CLI_SOURCE}' AND u.input_usd IS NOT NULL
        AND COALESCE(u.data_status,'complete') = 'complete'
        THEN 1 ELSE 0 END) AS estimated_requests,
    SUM(CASE WHEN u.source <> '{CLI_SOURCE}' AND u.input_usd IS NOT NULL
        AND COALESCE(u.data_status,'complete') = 'complete' AND u.cache_read_tokens IS NOT NULL
        THEN 1 ELSE 0 END) AS precise_estimate_requests,
    SUM(CASE WHEN u.source <> '{CLI_SOURCE}' AND u.input_usd IS NULL
        THEN 1 ELSE 0 END) AS unpriceable_requests,
    SUM(CASE WHEN COALESCE(u.data_status,'complete') = 'complete' THEN 1 ELSE 0 END) AS complete_requests,
    SUM(CASE WHEN u.data_status = 'partial' THEN 1 ELSE 0 END) AS partial_requests,
    SUM(CASE WHEN u.data_status = 'no-data' THEN 1 ELSE 0 END) AS nodata_requests,
    SUM(CASE WHEN u.data_status = 'errored' THEN 1 ELSE 0 END) AS errored_requests,
    SUM(CASE WHEN u.data_status = 'pending' THEN 1 ELSE 0 END) AS pending_requests,
    SUM(CASE WHEN COALESCE(u.data_status,'complete') IN ('complete','partial','prompt-only')
        THEN 1 ELSE 0 END) AS metered_requests,
    SUM(CASE WHEN u.source <> '{CLI_SOURCE}' AND COALESCE(u.rate_inferred,0) = 1
        THEN 1 ELSE 0 END) AS inferred_rate_requests
"""

SESSION_EMPTY_FILTER = (
    " HAVING SUM(COALESCE(u.total_nano_aiu,0)) > 0"
    " OR SUM(COALESCE(u.input_tokens,0)+COALESCE(u.output_tokens,0)) > 0"
)
SESSION_ORDER = (
    " ORDER BY SUM(COALESCE(u.total_nano_aiu,0)) DESC,"
    " SUM(COALESCE(u.input_tokens,0)+COALESCE(u.output_tokens,0)) DESC"
)


def build_filter(
    days: int | None = None,
    model: str | None = None,
    source: str | None = None,
    session_id: str | None = None,
) -> tuple[str, list[Any]]:
    """Build a parameterised WHERE clause from the active filters."""
    clauses: list[str] = []
    params: list[Any] = []
    if days:
        clauses.append(f"{LOCAL_DAY} >= date({LOCAL_TODAY}, ?)")
        params.append(f"-{int(days) - 1} days")
    if model:
        clauses.append("u.model = ?")
        params.append(model)
    if session_id:
        clauses.append("u.session_id = ?")
        params.append(session_id)
    if source:
        clauses.append("u.source = ?")
        params.append(source)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def session_sql(where: str, include_empty: bool = False, limit: int = 25) -> str:
    """Build the sessions query with ranking and total count."""
    return (
        f"SELECT u.session_id, {METRIC_COLUMNS},"
        " GROUP_CONCAT(DISTINCT u.model) AS models,"
        " MAX(u.created_at) AS last_used,"
        " (SELECT COUNT(*) FROM (SELECT 1 FROM assistant_usage_events u"
        f"{where} GROUP BY u.session_id)) AS total_sessions,"
        " MAX(s.summary) AS summary, MAX(s.repository) AS repository, MAX(s.branch) AS branch"
        " FROM assistant_usage_events u"
        " LEFT JOIN sessions s ON s.id = u.session_id"
        f"{where}"
        " GROUP BY u.session_id"
        f"{'' if include_empty else SESSION_EMPTY_FILTER}{SESSION_ORDER} LIMIT ?"
    )


def decorate(row: dict[str, Any]) -> dict[str, Any]:
    """Add derived cost and average fields to an aggregate row."""
    nano = row.pop("total_nano_aiu", 0) or 0
    requests = row.get("requests") or 0
    aiu = nano / NANO_PER_AIU
    row["aiu"] = round(aiu, 6)
    row["usd"] = round(aiu * USD_PER_AIU, 6)
    row["premium_requests"] = round(row.get("premium_requests") or 0, 2)
    row["avg_ttft_ms"] = round(row["avg_ttft_ms"], 1) if row.get("avg_ttft_ms") else None

    metered = row.get("metered_requests")
    metered = requests if metered is None else metered
    row["avg_total_tokens"] = round((row.get("total_tokens") or 0) / metered, 1) if metered else 0

    cached = row.get("cache_read_tokens") or 0
    prompt = row.get("prompt_tokens") or 0
    row["cache_hit_rate"] = round(cached / prompt, 4) if prompt else 0.0

    complete = row.get("complete_requests")
    if complete is not None:
        knowable = complete + (row.get("partial_requests") or 0) + (row.get("nodata_requests") or 0)
        row["knowable_requests"] = knowable
        row["token_coverage"] = round(complete / knowable, 4) if knowable else 1.0
        row["unmeasured_requests"] = max(knowable - complete, 0)

    priced = row.get("priced_requests")
    if priced is not None:
        row["priced_requests"] = priced
        row["unpriced_requests"] = max(requests - priced, 0)
        row["cost_covers_all"] = priced == requests
        estimated = row.get("estimated_requests") or 0
        for key in ("usd_floor", "usd_ceiling"):
            row[key] = round(row.get(key) or 0.0, 6)
        row["usd_is_range"] = estimated > 0 and row["usd_ceiling"] > row["usd_floor"]
    return row
