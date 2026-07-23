"""Machine idle state: sustained No-Load runs, per head (brief WP2).

A No-Load cycle is status 2 with zero torque -- the head cycles and the counter
advances, but no cap is applied. A *run* of them, longer than a threshold, is the
machine idling. This tool only became possible once the status semantics were
corrected: the old reading called status 2 an "OK cap", which would have made
idle time invisible.

Runs are found with the classic gaps-and-islands trick: number the rows per head,
number the no-load rows per head, and the difference is constant within a run.
"""
from analytics.result import ToolResult
from analytics.store import connect, scope_clause


def idle_periods(cfg, period=None, min_seconds=None):
    threshold = cfg.idle_min_seconds if min_seconds is None else min_seconds
    con = connect(cfg)
    where, params = scope_clause(cfg, period)

    rows = con.execute(f"""
        WITH marked AS (
            SELECT head_id, ts,
                   (status = ? AND app_torque = 0) AS no_load,
                   ROW_NUMBER() OVER (PARTITION BY head_id ORDER BY ts) AS rn_all,
                   ROW_NUMBER() OVER (
                       PARTITION BY head_id, (status = ? AND app_torque = 0) ORDER BY ts
                   ) AS rn_grp
            FROM cap_events
            WHERE {where}
        ),
        runs AS (
            SELECT head_id, rn_all - rn_grp AS island, MIN(ts) AS start, MAX(ts) AS end_ts,
                   COUNT(*) AS cycles
            FROM marked
            WHERE no_load
            GROUP BY head_id, island
        )
        SELECT head_id, start, end_ts, cycles,
               CAST(DATE_DIFF('second', start, end_ts) AS BIGINT) AS duration_seconds
        FROM runs
        WHERE DATE_DIFF('second', start, end_ts) >= ?
        ORDER BY head_id, start
    """, [cfg.no_load_status, cfg.no_load_status] + params + [threshold]).fetchall()

    if not rows:
        return ToolResult.insufficient(
            "idle_periods",
            f"no idle periods of >= {threshold}s in period {period!r}",
            period=period,
        )

    periods = [
        {"head_id": int(r[0]), "start": r[1], "end": r[2],
         "cycles": r[3], "duration_seconds": int(r[4])}
        for r in rows
    ]
    return ToolResult.ok(
        "idle_periods",
        {"periods": periods,
         "total_idle_seconds": sum(p["duration_seconds"] for p in periods)},
        period=period,
        rows_scanned=sum(p["cycles"] for p in periods),
        filters=[f"min_seconds={threshold}"],
        assumptions=["an idle period is a sustained run of no-load cycles (status 2, torque 0)"],
    )
