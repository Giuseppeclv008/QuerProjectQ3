"""Closures that match a filter the operator names: a torque limit, an outcome, a head.

"How many closures had torque above X Nm?", "list the failed events below a
threshold", "all failed events of head 3" are the brief's filtering queries, and
none of the other tools can answer them: `anomalies` counts against the
configured band, not against a value the operator gives, and nothing listed
individual events. This one counts exactly and lists the first few.

Only closures with load are counted, as everywhere else, and a limit is strict:
"above 2.5" excludes 2.5. The limit is compared as the REAL the column holds, so
a stored 2.2 is not "above" a limit of 2.2.
"""
from analytics.result import ToolResult
from analytics.status import REJECT_SQL, decode
from analytics.store import connect, scope_clause
from analytics.tools.overview import ASSUMPTION

_OUTCOMES = ("successful", "failed", "all")
LISTED = 20          # events itemised; the count is exact whatever this is


def closure_filter(cfg, period=None, above=None, below=None, outcome="all", head=None):
    if outcome not in _OUTCOMES:
        return ToolResult.error(
            "closure_filter", f"outcome must be one of {list(_OUTCOMES)}, got {outcome!r}",
            period=period)
    if head is not None and head < 1:
        return ToolResult.error(
            "closure_filter", f"head must be a head number from 1, got {head!r}",
            period=period)

    con = connect(cfg)
    where, params = scope_clause(cfg, period)

    conds, sem = ["app_torque > 0"], []
    if above is not None:
        conds.append("app_torque > CAST(? AS REAL)")
        sem.append(above)
    if below is not None:
        conds.append("app_torque < CAST(? AS REAL)")
        sem.append(below)
    if head is not None:
        conds.append("head_id = ?")
        sem.append(head)
    if outcome == "successful":
        conds.append("status = ?")
        sem.append(cfg.success_status)
    elif outcome == "failed":
        conds.append(REJECT_SQL)
    base = f"FROM cap_events WHERE {' AND '.join(conds)} AND {where}"

    in_scope = con.execute(
        f"SELECT COUNT(*) FROM cap_events WHERE app_torque > 0 AND {where}", params
    ).fetchone()[0]
    if in_scope == 0:
        return ToolResult.insufficient(
            "closure_filter", f"no capping operations in period {period!r}", period=period)

    count = con.execute(f"SELECT COUNT(*) {base}", sem + params).fetchone()[0]
    by_head = [
        {"head_id": int(h), "count": int(n)}
        for h, n in con.execute(
            f"SELECT head_id, COUNT(*) {base} GROUP BY head_id "
            f"ORDER BY COUNT(*) DESC, head_id", sem + params).fetchall()
    ]
    events = [
        {"head_id": int(r[0]), "ts": r[1], "app_torque": r[2], "status": r[3],
         "outcome": ("rejected: " + ", ".join(decode(r[3])["conditions"] or ["unspecified"])
                     if int(r[3]) % 2 else "ok" if r[3] == cfg.success_status
                     else "no verdict")}
        for r in con.execute(
            f"SELECT head_id, ts, app_torque, status {base} "
            f"ORDER BY ts, cap_seq LIMIT {LISTED}", sem + params).fetchall()
    ]
    return ToolResult.ok(
        "closure_filter",
        {"filters": {"above": above, "below": below, "outcome": outcome, "head": head},
         "count": count,
         "capping_operations": in_scope,
         "by_head": by_head,
         "events": events,
         "listed": len(events)},
        period=period,
        rows_scanned=in_scope,
        filters=[f"above={above}", f"below={below}", f"outcome={outcome}", f"head={head}"],
        assumptions=[
            ASSUMPTION,
            "a torque limit is strict (above 2.5 excludes 2.5) and is compared "
            "as the REAL the store holds",
            f"the count is exact; at most {LISTED} events are listed, in time order",
        ],
    )
