"""Trend and drift (brief WP2: moving averages, drift detection).

This is where the real signal lives. The machine's success rate is ~99.999%, so
counting failures says almost nothing; a head whose torque is slowly *walking*
away from its baseline is the finding that matters for the predictive maintenance
the brief names as motivation.

Drift uses the Mann-Kendall rank correlation (tau), not a linear regression:
it is non-parametric, makes no assumption that the walk is linear or the noise
Gaussian, and is deterministic. tau = +1 means every day rose on the previous;
-1 means every day fell.
"""
import pandas as pd

from analytics.result import ToolResult
from analytics.store import connect, scope_clause

_SIGNALS = ("torque", "success_rate")
_BUCKETS = {"day": "DAY", "hour": "HOUR"}
DRIFT_TAU = 0.5     # |tau| at or above this counts as drifting


def mann_kendall_tau(values):
    """Kendall's tau-a: (concordant - discordant) / (n*(n-1)/2). Range [-1, 1]."""
    n = len(values)
    if n < 2:
        return 0.0
    s = 0
    for i in range(n - 1):
        for j in range(i + 1, n):
            if values[j] > values[i]:
                s += 1
            elif values[j] < values[i]:
                s -= 1
    return s / (n * (n - 1) / 2)


def trend(cfg, period=None, signal="torque", by="day", window=7):
    if signal not in _SIGNALS:
        return ToolResult.error("trend", f"signal must be one of {list(_SIGNALS)}, "
                                         f"got {signal!r}", period=period)
    if by not in _BUCKETS:
        return ToolResult.error("trend", f"by must be one of {sorted(_BUCKETS)}, "
                                         f"got {by!r}", period=period)
    if window < 1:
        return ToolResult.error("trend", f"window must be >= 1, got {window!r}",
                                period=period)

    con = connect(cfg)
    where, params = scope_clause(cfg, period)
    unit = _BUCKETS[by]

    # success_rate = successful / (successful + failed), the locked spec §3.2
    # denominator that success_rates() uses -- caps whose status is neither success
    # nor fault are not a verdict and must not dilute the rate. NULLIF(..., 0) makes a
    # bucket with no verdicts undefined (NULL -> None), never a division error.
    expr = ("AVG(app_torque)" if signal == "torque"
            else "COUNT(*) FILTER (WHERE status = ?) * 1.0 "
                 "/ NULLIF(COUNT(*) FILTER (WHERE status IN (?, ?)), 0)")
    sem = ([] if signal == "torque"
           else [cfg.success_status, cfg.success_status, cfg.fault_status])

    rows = con.execute(f"""
        SELECT head_id, DATE_TRUNC('{unit}', ts) AS bucket, {expr} AS value
        FROM cap_events
        WHERE app_torque > 0 AND {where}
        GROUP BY 1, 2
        HAVING COUNT(*) > 0
        ORDER BY 1, 2
    """, sem + params).fetchall()

    if not rows:
        return ToolResult.insufficient(
            "trend", f"no capping operations in period {period!r}", period=period
        )

    df = pd.DataFrame(rows, columns=["head_id", "bucket", "value"])
    df["rolling_mean"] = df.groupby("head_id")["value"].transform(
        lambda s: s.rolling(window, min_periods=1).mean()
    )
    df["rolling_std"] = df.groupby("head_id")["value"].transform(
        lambda s: s.rolling(window, min_periods=1).std()
    )

    drift = []
    for head, grp in df.groupby("head_id"):
        # A verdictless success_rate bucket is NaN (None); drop it before ranking so
        # it neither concords nor discords. Torque values are never NaN.
        tau = mann_kendall_tau([v for v in grp["value"] if pd.notna(v)])
        drift.append({
            "head_id": int(head),
            "tau": tau,
            "direction": "rising" if tau > 0 else "falling" if tau < 0 else "flat",
            "drifting": bool(abs(tau) >= DRIFT_TAU),
        })
    drift.sort(key=lambda d: -abs(d["tau"]))

    series = [
        {"head_id": int(r.head_id), "bucket": r.bucket,
         "value": None if pd.isna(r.value) else float(r.value),
         "rolling_mean": None if pd.isna(r.rolling_mean) else float(r.rolling_mean),
         "rolling_std": None if pd.isna(r.rolling_std) else float(r.rolling_std)}
        for r in df.itertuples()
    ]

    # rows_scanned is the provenance denominator: capping operations examined in
    # scope, not the number of aggregated series points.
    scanned = con.execute(
        f"SELECT COUNT(*) FROM cap_events WHERE app_torque > 0 AND {where}", params
    ).fetchone()[0]
    return ToolResult.ok(
        "trend", {"series": series, "drift": drift},
        period=period, rows_scanned=scanned,
        filters=[f"signal={signal}", f"by={by}", f"window={window}"],
        assumptions=[f"drift is Mann-Kendall |tau| >= {DRIFT_TAU} over the per-head "
                     f"{by} series (non-parametric: assumes neither linearity nor "
                     "Gaussian noise)"],
    )
