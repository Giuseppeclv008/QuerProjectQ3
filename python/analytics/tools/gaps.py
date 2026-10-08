"""Holes in the machine's event stream: the closest the store gets to downtime.

A row exists only when a head's counter advanced, so a stopped machine writes
nothing at all -- which is why idle_periods (no-load cycling) cannot see it.
What a stop does leave is a hole: a stretch of time in which no head advanced.
This tool measures those holes over the whole machine, on the distinct poll
timestamps, with LAG.

What a hole means is stated rather than assumed. Silence is the machine
stopped OR its data not arriving -- the store cannot tell the two apart. Time
before the period's first event and after its last is not a hole: there is no
edge to measure from. And a hole belongs to the machine, never to a head --
every head stops together, so no head is named as the cause.
"""
from analytics.result import ToolResult
from analytics.store import connect, scope_clause


def event_gaps(cfg, period=None, min_seconds=None):
    threshold = cfg.idle_max_gap_seconds if min_seconds is None else min_seconds
    con = connect(cfg)
    where, params = scope_clause(cfg, period)

    scanned = con.execute(
        f"SELECT COUNT(*) FROM cap_events WHERE {where}", params
    ).fetchone()[0]
    if scanned == 0:
        return ToolResult.insufficient(
            "event_gaps", f"no events in period {period!r}", period=period)

    rows = con.execute(f"""
        WITH polls AS (
            SELECT DISTINCT ts FROM cap_events WHERE {where}
        ),
        stepped AS (
            SELECT LAG(ts) OVER (ORDER BY ts) AS prev_ts, ts FROM polls
        )
        SELECT prev_ts, ts,
               CAST(DATE_DIFF('second', prev_ts, ts) AS BIGINT) AS seconds
        FROM stepped
        WHERE prev_ts IS NOT NULL AND DATE_DIFF('second', prev_ts, ts) > ?
        ORDER BY prev_ts
    """, params + [threshold]).fetchall()

    gaps = [{"start": r[0], "end": r[1], "duration_seconds": int(r[2])}
            for r in rows]
    total = sum(g["duration_seconds"] for g in gaps)
    longest = max(gaps, key=lambda g: g["duration_seconds"]) if gaps else None
    return ToolResult.ok(
        "event_gaps",
        {"gaps": gaps,
         "gap_count": len(gaps),
         "total_gap_seconds": total,
         "total_gap_hours": total / 3600.0,
         "longest_gap": longest,
         "min_seconds": threshold},
        period=period,
        rows_scanned=scanned,
        filters=[f"min_seconds={threshold}"],
        assumptions=[
            f"a gap is more than {threshold}s with no event from any head; "
            "silence means the machine was stopped OR its data did not arrive, "
            "and the store cannot tell the two apart",
            "time before the period's first event and after its last is not "
            "counted as a gap",
            "gaps belong to the whole machine and are never attributed to a head",
        ],
    )
