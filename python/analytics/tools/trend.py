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
import math
from collections import Counter

import numpy as np
import pandas as pd

from analytics.result import ToolResult
from analytics.status import REJECT_SQL
from analytics.store import connect, scope_clause

_SIGNALS = ("torque", "success_rate")
_BUCKETS = {"day": "DAY", "hour": "HOUR"}
DRIFT_TAU = 0.5     # |tau| at or above this counts as drifting...
DRIFT_P = 0.05      # ...but only when the trend is also significant at this level
MIN_DRIFT_BUCKETS = 8   # below this the normal approximation for p is invalid
_TAU_CHUNK = 512    # rows per block; bounds the temporary at ~chunk*n*8 bytes


def _mann_kendall_s(values):
    """Kendall's S: sum over i<j of sign(v[j] - v[i]).

    This was a doubly-nested Python loop. With by="day" over a month n is ~28 and
    it never mattered, but by="hour" is exposed to the model in the tool registry
    and three months of hourly buckets is n ~ 2,160 per head: ~2.3M comparisons
    times 36 heads, roughly 84M interpreter iterations for one natural-language
    question, with no cap and no timeout.

    Same arithmetic, vectorised. Blocked over rows rather than materialising the
    whole n x n sign matrix, so peak memory stays near 9 MB at n = 2,160 instead
    of 37 MB and grows linearly in n rather than quadratically.
    """
    n = len(values)
    v = np.asarray(values, dtype=np.float64)
    cols = np.arange(n)
    s = 0.0
    for start in range(0, n - 1, _TAU_CHUNK):
        rows = np.arange(start, min(start + _TAU_CHUNK, n - 1))
        block = np.sign(v[None, :] - v[rows, None])          # (k, n)
        s += float(block[cols[None, :] > rows[:, None]].sum())
    return s


def mann_kendall_tau(values):
    """Kendall's tau-b: S / sqrt(D * (D - T)), tie-corrected. Range [-1, 1].

    Tau-b, not tau-a, because mann_kendall_p below applies the tie correction
    to Var(S): pairing an untied effect size with a tie-corrected significance
    test shrank tau on exactly the signals ties dominate (success_rate is
    mostly exact 1.0 buckets), which made the |tau| >= 0.5 drift gate
    undocumentedly stricter on that signal than on torque. The time axis is
    the untied bucket index, so only the value ties enter the correction.
    With no ties, tau-b equals tau-a.
    """
    n = len(values)
    if n < 2:
        return 0.0
    d = n * (n - 1) / 2
    ties = Counter(float(v) for v in values).values()
    t = sum(tj * (tj - 1) / 2 for tj in ties)
    if d - t <= 0:      # every value tied: no trend to measure
        return 0.0
    return _mann_kendall_s(values) / math.sqrt(d * (d - t))


def mann_kendall_p(values):
    """Two-sided p-value for the Mann-Kendall test, normal approximation with
    tie correction and continuity correction.

    Var(S) = [n(n-1)(2n+5) - sum_j t_j(t_j-1)(2t_j+5)] / 18 over tie groups of
    size t_j. The approximation is conventionally trusted from n ~ 8-10 upward;
    callers below MIN_DRIFT_BUCKETS should not ask (trend() does not).

    tau alone is not evidence: with n=2 buckets tau is +/-1 whenever the value
    moves at all, and a bare |tau| >= 0.5 rule flagged every head of a store of
    pure noise as drifting, with a maintenance action recommended.
    """
    n = len(values)
    if n < 2:
        return 1.0
    s = _mann_kendall_s(values)
    ties = Counter(float(v) for v in values).values()
    var_s = (n * (n - 1) * (2 * n + 5)
             - sum(t * (t - 1) * (2 * t + 5) for t in ties)) / 18.0
    if var_s <= 0:      # every value tied: no trend, no evidence
        return 1.0
    if s > 0:
        z = (s - 1) / math.sqrt(var_s)
    elif s < 0:
        z = (s + 1) / math.sqrt(var_s)
    else:
        z = 0.0
    return math.erfc(abs(z) / math.sqrt(2))


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
                 f"/ NULLIF(COUNT(*) FILTER (WHERE status = ? OR {REJECT_SQL}), 0)")
    sem = ([] if signal == "torque"
           else [cfg.success_status, cfg.success_status])

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
        vals = [v for v in grp["value"] if pd.notna(v)]
        tau = mann_kendall_tau(vals)
        # "drifting" is a claim about the machine, so it needs both effect size
        # (|tau|) and evidence (p). Below MIN_DRIFT_BUCKETS the p approximation
        # is invalid and the entry says "insufficient" instead of guessing --
        # the canned by="day" plan lands here for any period under 8 active days.
        if len(vals) < MIN_DRIFT_BUCKETS:
            p = None
            verdict = False
            insufficient = True
        else:
            p = mann_kendall_p(vals)
            verdict = bool(abs(tau) >= DRIFT_TAU and p < DRIFT_P)
            insufficient = False
        drift.append({
            "head_id": int(head),
            "tau": tau,
            "p_value": p,
            "n_buckets": len(vals),
            "direction": "rising" if tau > 0 else "falling" if tau < 0 else "flat",
            "drifting": verdict,
            "insufficient": insufficient,
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
        assumptions=[f"drift is Mann-Kendall |tau| >= {DRIFT_TAU} AND p < {DRIFT_P} "
                     f"(tie-corrected normal approximation) over the per-head {by} "
                     "series (non-parametric: assumes neither linearity nor "
                     "Gaussian noise)",
                     f"heads with fewer than {MIN_DRIFT_BUCKETS} {by} buckets get no "
                     "drift verdict (insufficient=true): the significance "
                     "approximation is invalid there and tau alone is not evidence"],
    )
