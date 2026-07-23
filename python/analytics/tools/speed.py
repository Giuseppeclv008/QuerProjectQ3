"""Capping speed in pieces/hour (brief WP1).

The brief describes this as an "incremental average" computed during ingestion.
We compute it in SQL over the persisted events instead: it is the same number --
caps per unit time -- and needs no schema column, no migration, and no
reprocessing of the 89 day-files. Only closures WITH load produce a piece.
"""
from analytics.result import ToolResult
from analytics.store import connect, scope_clause
from analytics.tools.overview import ASSUMPTION

_BUCKETS = {"hour": ("HOUR", 1.0), "day": ("DAY", 24.0)}


def capping_speed(cfg, period=None, bucket="hour"):
    if bucket not in _BUCKETS:
        return ToolResult.error(
            "capping_speed", f"bucket must be one of {sorted(_BUCKETS)}, got {bucket!r}",
            period=period,
        )
    unit, hours_per_bucket = _BUCKETS[bucket]

    con = connect(cfg)
    where, params = scope_clause(cfg, period)

    rows = con.execute(f"""
        SELECT DATE_TRUNC('{unit}', ts) AS bucket_start, COUNT(*) AS caps
        FROM cap_events
        WHERE app_torque > 0 AND {where}
        GROUP BY 1 ORDER BY 1
    """, params).fetchall()

    if not rows:
        return ToolResult.insufficient(
            "capping_speed", f"no capping operations in period {period!r}", period=period
        )

    buckets = [
        {"bucket_start": r[0], "caps": r[1], "pieces_per_hour": r[1] / hours_per_bucket}
        for r in rows
    ]
    total = sum(b["caps"] for b in buckets)
    return ToolResult.ok(
        "capping_speed",
        {
            "buckets": buckets,
            "mean_pieces_per_hour": sum(b["pieces_per_hour"] for b in buckets) / len(buckets),
        },
        period=period, rows_scanned=total,
        filters=[f"bucket={bucket}", "app_torque > 0 (only real caps produce pieces)"],
        assumptions=[ASSUMPTION],
    )
