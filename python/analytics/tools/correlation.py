"""Head-vs-head correlation (brief WP2).

Answers "compare head 1 and head 5" and, more usefully, "which capping head
behaves differently from the others?" -- a head whose mean correlation against
its peers is lowest is the one out of step with the machine.

Correlation is computed on the per-head bucketed torque series, so two heads
correlate when their torque moves together over time. A single bucket cannot
produce a correlation, so that is insufficient_data rather than a NaN.
"""
import pandas as pd

from analytics.result import ToolResult
from analytics.store import connect, discover_heads, scope_clause

_BUCKETS = {"day": "DAY", "hour": "HOUR"}


def head_correlation(cfg, period=None, heads=None, by="day"):
    if by not in _BUCKETS:
        return ToolResult.error("head_correlation", f"by must be one of {sorted(_BUCKETS)}, "
                                                    f"got {by!r}", period=period)
    con = connect(cfg)
    where, params = scope_clause(cfg, period)
    unit = _BUCKETS[by]

    if heads is None:
        heads = discover_heads(con, cfg)          # never assumes 36
    if len(heads) < 2:
        return ToolResult.insufficient(
            "head_correlation", f"need at least 2 heads, got {len(heads)}", period=period
        )

    placeholders = ",".join("?" for _ in heads)
    rows = con.execute(f"""
        SELECT head_id, DATE_TRUNC('{unit}', ts) AS bucket, AVG(app_torque) AS value
        FROM cap_events
        WHERE app_torque > 0 AND head_id IN ({placeholders}) AND {where}
        GROUP BY 1, 2 ORDER BY 2
    """, list(heads) + params).fetchall()

    if not rows:
        return ToolResult.insufficient(
            "head_correlation", f"no capping operations in period {period!r}", period=period
        )

    df = pd.DataFrame(rows, columns=["head_id", "bucket", "value"])
    wide = df.pivot(index="bucket", columns="head_id", values="value")
    if len(wide) < 2:
        return ToolResult.insufficient(
            "head_correlation",
            f"need at least 2 {by} buckets to correlate, got {len(wide)}",
            period=period, rows_scanned=len(rows),
        )

    corr = wide.corr()
    matrix = {
        int(a): {int(b): (None if pd.isna(corr.loc[a, b]) else float(corr.loc[a, b]))
                 for b in corr.columns}
        for a in corr.index
    }

    outliers = []
    for head, row in corr.iterrows():
        peers = row.drop(labels=[head]).dropna()
        if len(peers):
            outliers.append({"head_id": int(head), "mean_correlation": float(peers.mean())})
    outliers.sort(key=lambda o: o["mean_correlation"])

    return ToolResult.ok(
        "head_correlation", {"matrix": matrix, "outliers": outliers},
        period=period, rows_scanned=len(rows),
        filters=[f"heads={sorted(heads)}", f"by={by}"],
        assumptions=["heads correlate on their bucketed mean torque series; the head "
                     "with the lowest mean correlation to its peers is the one out of step"],
    )
