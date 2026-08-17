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

# Pearson over n=2 shared points is +/-1 by construction -- every pair "correlates
# perfectly", the whole ranking ties, and the report claimed all heads track each
# other closely at whatever sign the noise happened to give. Three is the floor at
# which the coefficient can disagree with itself; it is still weak evidence, and
# the assumption below says so.
MIN_BUCKETS = 3


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

    # rows_scanned is the provenance denominator: capping operations examined in
    # scope, not the count of (head, bucket) aggregate points.
    scanned = con.execute(
        f"SELECT COUNT(*) FROM cap_events "
        f"WHERE app_torque > 0 AND head_id IN ({placeholders}) AND {where}",
        list(heads) + params,
    ).fetchone()[0]

    df = pd.DataFrame(rows, columns=["head_id", "bucket", "value"])
    wide = df.pivot(index="bucket", columns="head_id", values="value")
    if len(wide) < MIN_BUCKETS:
        return ToolResult.insufficient(
            "head_correlation",
            f"need at least {MIN_BUCKETS} {by} buckets to correlate, got {len(wide)}",
            period=period, rows_scanned=scanned,
        )

    # min_periods guards the pairwise path: DataFrame.corr() uses
    # pairwise-complete observations, so a head present in only 2 of many
    # buckets got a +/-1 against every peer -- and topped the outlier ranking.
    corr = wide.corr(min_periods=MIN_BUCKETS)
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
        period=period, rows_scanned=scanned,
        filters=[f"heads={sorted(heads)}", f"by={by}"],
        assumptions=["heads correlate on their bucketed mean torque series; the head "
                     "with the lowest mean correlation to its peers is the one out "
                     "of step *in shape*",
                     "Pearson correlation is invariant to a per-head affine shift, so "
                     "this ranking cannot see a level offset: a head running "
                     "consistently below the others while moving with them scores ~1 "
                     "and is reported as tracking. Compare per-head median torque "
                     "(torque_stats by head) to rule that out",
                     "a head with no defined correlation to any peer (constant torque, "
                     f"zero variance, or fewer than {MIN_BUCKETS} shared buckets) is "
                     "omitted from outliers and shown as None in the matrix",
                     f"{MIN_BUCKETS} buckets is a floor, not power: treat correlations "
                     "over few buckets as suggestive only"],
    )
