"""Success rates — the brief's flagship KPI (slides 16-17).

A head that only ever cycled with no load has performed zero capping operations.
It is omitted rather than reported at 0%: a fabricated zero would read as a
catastrophically failing head when in truth nothing was ever capped.
"""
from analytics.result import ToolResult
from analytics.store import connect, scope_clause
from analytics.tools.overview import ASSUMPTION

_GROUPS = {"head": "head_id", "day": "CAST(ts AS DATE)", "overall": None}


def success_rates(cfg, period=None, by="head"):
    if by not in _GROUPS:
        return ToolResult.error(
            "success_rates", f"by must be one of {sorted(_GROUPS)}, got {by!r}", period=period
        )

    con = connect(cfg)
    where, params = scope_clause(cfg, period)
    sem = [cfg.success_status, cfg.fault_status]

    # Only closures WITH load are capping operations (spec §3.2).
    select = """
        COUNT(*)                                  AS total,
        COUNT(*) FILTER (WHERE status = ?)        AS successful,
        COUNT(*) FILTER (WHERE status = ?)        AS failed
    """
    base = f"FROM cap_events WHERE app_torque > 0 AND {where}"

    if by == "overall":
        row = con.execute(f"SELECT {select} {base}", sem + params).fetchone()
        if row[0] == 0:
            return ToolResult.insufficient(
                "success_rates", f"no capping operations in period {period!r}", period=period
            )
        per_head = con.execute(
            f"""SELECT head_id, {select} {base} GROUP BY head_id
                HAVING COUNT(*) > 0 ORDER BY 4 DESC, head_id""",
            sem + params,
        ).fetchall()
        lowest = min(per_head, key=lambda h: (h[2] / h[1], h[0]))[0] if per_head else None
        values = {
            "total": row[0],
            "successful": row[1],
            "failed": row[2],
            "success_rate": row[1] / row[0],
            "lowest_head": int(lowest) if lowest is not None else None,
        }
    else:
        group = _GROUPS[by]
        label = "head_id" if by == "head" else "day"
        rows = con.execute(
            f"""SELECT {group} AS {label}, {select} {base}
                GROUP BY 1 ORDER BY 1""",
            sem + params,
        ).fetchall()
        if not rows:
            return ToolResult.insufficient(
                "success_rates", f"no capping operations in period {period!r}", period=period
            )
        values = [
            {
                label: int(r[0]) if by == "head" else r[0],
                "total": r[1],
                "successful": r[2],
                "failed": r[3],
                "success_rate": r[2] / r[1],
            }
            for r in rows
        ]

    scanned = sum(v["total"] for v in values) if isinstance(values, list) else values["total"]
    return ToolResult.ok(
        "success_rates", values,
        period=period, rows_scanned=scanned,
        filters=["app_torque > 0 (capping operations only)"],
        assumptions=[ASSUMPTION],
    )
