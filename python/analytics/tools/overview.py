"""Dataset exploration + WP1 validation checks.

Answers the brief's exploration queries: how many capping operations, what time
range, any missing or invalid torque. The headline distinction it enforces
everywhere: a *capping operation* is a closure WITH load. No-load cycles (the
counter advances, no cap is applied) are counted separately and never inflate a
success denominator (spec §3.2).
"""
from analytics.result import ToolResult
from analytics.status import REJECT_SQL
from analytics.store import connect, discover_heads, scope_clause

ASSUMPTION = (
    "a capping operation is a closure with torque > 0; no-load cycles "
    "(status 2, torque 0) are excluded from success denominators"
)


def overview(cfg, period=None):
    con = connect(cfg)
    where, params = scope_clause(cfg, period)

    row = con.execute(f"""
        SELECT
            COUNT(*)                                                    AS closures,
            COUNT(*) FILTER (WHERE app_torque > 0)                      AS capping_operations,
            COUNT(*) FILTER (WHERE status = ? AND app_torque > 0)       AS successful,
            COUNT(*) FILTER (WHERE {REJECT_SQL} AND app_torque > 0)     AS failed,
            COUNT(*) FILTER (WHERE status = ? AND app_torque = 0)       AS no_load,
            MIN(ts)                                                     AS ts_min,
            MAX(ts)                                                     AS ts_max,
            COUNT(*) FILTER (WHERE app_torque IS NULL)                  AS null_torque,
            COUNT(*) FILTER (WHERE app_torque > 0
                             AND (app_torque < ? OR app_torque > ?))    AS invalid_torque,
            COUNT(*) FILTER (WHERE is_reset)                            AS counter_resets
        FROM cap_events
        WHERE {where}
    """, [cfg.success_status, cfg.no_load_status,
          cfg.torque_min, cfg.torque_max] + params).fetchone()

    closures = row[0]
    if closures == 0:
        return ToolResult.insufficient(
            "overview", f"no capping events in period {period!r}", period=period
        )

    heads = con.execute(
        f"SELECT DISTINCT head_id FROM cap_events WHERE {where} ORDER BY head_id", params
    ).fetchall()

    return ToolResult.ok(
        "overview",
        {
            "capping_operations": row[1],
            "successful": row[2],
            "failed": row[3],
            "no_load_cycles": row[4],
            "heads": [int(h[0]) for h in heads],
            "ts_min": row[5],
            "ts_max": row[6],
            "null_torque": row[7],
            "invalid_torque": row[8],
            "counter_resets": row[9],
        },
        period=period,
        rows_scanned=closures,
        filters=[f"period={period}"] if period else [],
        assumptions=[ASSUMPTION],
    )
