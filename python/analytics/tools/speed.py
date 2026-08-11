"""Capping speed in pieces/hour (brief WP1).

The brief describes this as an "incremental average" computed during ingestion.
We compute it in SQL over the persisted events instead: it is the same number --
caps per unit time -- and needs no schema column, no migration, and no
reprocessing of the 89 day-files. Only closures WITH load produce a piece.

`mean_pieces_per_hour` is the mean over ACTIVE buckets only: the GROUP BY never
emits a bucket with zero capping operations, so an idle hour or day does not pull
the mean down. It is a typical active-bucket rate, not total pieces over elapsed
time -- the returned `assumptions` say so, and a multi-bucket test pins it.
"""
from analytics.result import ToolResult
from analytics.store import connect, scope_clause
from analytics.tools.overview import ASSUMPTION

_BUCKETS = {"hour": "HOUR", "day": "DAY"}


def capping_speed(cfg, period=None, bucket="hour"):
    if bucket not in _BUCKETS:
        return ToolResult.error(
            "capping_speed", f"bucket must be one of {sorted(_BUCKETS)}, got {bucket!r}",
            period=period,
        )
    unit = _BUCKETS[bucket]

    con = connect(cfg)
    where, params = scope_clause(cfg, period)

    # The denominator is the number of hours in the bucket that actually saw a
    # closure, not the bucket's calendar length. With bucket="day" the old code
    # divided by a fixed 24.0, so the KPI report's headline "pieces/hour" was
    # really pieces-per-day over 24 -- while idle_periods reported 7,486 idle
    # head-hours in the same month. Counting active hours makes the number what
    # its name says, and AROL define production speed as bottles closed per unit
    # time (material/various.txt), so pieces_per_second is reported too.
    rows = con.execute(f"""
        SELECT DATE_TRUNC('{unit}', ts) AS bucket_start,
               COUNT(*) AS caps,
               COUNT(DISTINCT DATE_TRUNC('HOUR', ts)) AS active_hours
        FROM cap_events
        WHERE app_torque > 0 AND {where}
        GROUP BY 1 ORDER BY 1
    """, params).fetchall()

    if not rows:
        return ToolResult.insufficient(
            "capping_speed", f"no capping operations in period {period!r}", period=period
        )

    buckets = [
        {"bucket_start": r[0], "caps": r[1], "active_hours": r[2],
         "pieces_per_hour": r[1] / r[2] if r[2] else 0.0,
         "pieces_per_second": (r[1] / r[2] / 3600.0) if r[2] else 0.0}
        for r in rows
    ]
    total = sum(b["caps"] for b in buckets)
    return ToolResult.ok(
        "capping_speed",
        {
            "buckets": buckets,
            "mean_pieces_per_hour": sum(b["pieces_per_hour"] for b in buckets) / len(buckets),
            "mean_pieces_per_second":
                sum(b["pieces_per_second"] for b in buckets) / len(buckets),
        },
        period=period, rows_scanned=total,
        filters=[f"bucket={bucket}", "app_torque > 0 (only real caps produce pieces)"],
        assumptions=[
            ASSUMPTION,
            "rate = closures / hours that actually saw a closure in the bucket, not "
            "/ the bucket's calendar length; a day with 10 productive hours is not "
            "divided by 24",
            "buckets with zero capping operations are never emitted, so a fully "
            "idle hour or day does not pull the mean down",
        ],
    )
