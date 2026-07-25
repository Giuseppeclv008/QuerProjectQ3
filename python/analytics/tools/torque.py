"""Torque statistics (brief slide 18).

Always filtered to closures WITH load. This is not a detail: 337,772 no-load
cycles a day carry torque 0.0, and including them would drag every mean toward
zero and invent a bimodal distribution that does not exist.
"""
from analytics.result import ToolResult
from analytics.status import REJECT_SQL
from analytics.store import connect, scope_clause
from analytics.tools.overview import ASSUMPTION

_OUTCOMES = ("successful", "failed", "all")


def torque_stats(cfg, period=None, outcome="successful", by=None):
    if outcome not in _OUTCOMES:
        return ToolResult.error(
            "torque_stats", f"outcome must be one of {list(_OUTCOMES)}, got {outcome!r}",
            period=period,
        )
    if by not in (None, "head"):
        return ToolResult.error("torque_stats", f"by must be None or 'head', got {by!r}",
                                period=period)

    con = connect(cfg)
    where, params = scope_clause(cfg, period)

    cond, sem = "app_torque > 0", []
    if outcome == "successful":
        cond, sem = "app_torque > 0 AND status = ?", [cfg.success_status]
    elif outcome == "failed":
        cond, sem = REJECT_SQL, []

    agg = """
        COUNT(*)          AS n,
        AVG(app_torque)   AS mean,
        MIN(app_torque)   AS min,
        MAX(app_torque)   AS max,
        STDDEV_SAMP(app_torque) AS stddev,
        MEDIAN(app_torque)      AS median
    """
    base = f"FROM cap_events WHERE {cond} AND {where}"

    if by == "head":
        rows = con.execute(
            f"SELECT head_id, {agg} {base} GROUP BY head_id ORDER BY stddev DESC NULLS LAST, head_id",
            sem + params,
        ).fetchall()
        if not rows:
            return ToolResult.insufficient(
                "torque_stats", f"no {outcome} closures in period {period!r}", period=period
            )
        values = [
            {"head_id": int(r[0]), "n": r[1], "mean": r[2], "min": r[3],
             "max": r[4], "stddev": r[5] or 0.0, "median": r[6]}
            for r in rows
        ]
        scanned = sum(v["n"] for v in values)
    else:
        r = con.execute(f"SELECT {agg} {base}", sem + params).fetchone()
        if r[0] == 0:
            return ToolResult.insufficient(
                "torque_stats", f"no {outcome} closures in period {period!r}", period=period
            )
        values = {"n": r[0], "mean": r[1], "min": r[2], "max": r[3],
                  "stddev": r[4] or 0.0, "median": r[5]}
        scanned = r[0]

    return ToolResult.ok(
        "torque_stats", values,
        period=period, rows_scanned=scanned,
        filters=[f"outcome={outcome}", "app_torque > 0 (no-load excluded)"],
        assumptions=[ASSUMPTION],
    )
