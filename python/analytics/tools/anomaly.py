"""Deterministic anomaly detection: thresholds + robust statistical deviation.

The brief asks for "simple anomaly detection (thresholds, statistical
deviations)" and requires WP2 to be deterministic so the agent has reliable
tools to call. No model, no training, no randomness -- the same data always
yields the same flags.

The deviation band is median +/- k*MAD rather than mean +/- k*sigma: a handful of
extreme outliers inflate sigma enough to hide themselves, and MAD does not have
that problem.

Note the two methods are complementary, not redundant: threshold catches torque
outside the *machine's* spec band; deviation catches a head drifting away from
*its own* normal, even while still inside the band.
"""
from analytics.result import ToolResult
from analytics.store import connect, scope_clause

_METHODS = ("threshold", "deviation", "both")


def anomalies(cfg, period=None, method="both"):
    if method not in _METHODS:
        return ToolResult.error(
            "anomalies", f"method must be one of {list(_METHODS)}, got {method!r}",
            period=period,
        )

    con = connect(cfg)
    where, params = scope_clause(cfg, period)

    faults = [
        {"head_id": int(r[0]), "ts": r[1], "app_torque": r[2], "reason": "fault status"}
        for r in con.execute(
            f"""SELECT head_id, ts, app_torque FROM cap_events
                WHERE status = ? AND {where} ORDER BY ts""",
            [cfg.fault_status] + params,
        ).fetchall()
    ]

    threshold_hits = []
    if method in ("threshold", "both"):
        threshold_hits = [
            {"head_id": int(r[0]), "ts": r[1], "app_torque": r[2],
             "reason": f"torque outside band [{cfg.torque_min}, {cfg.torque_max}]"}
            for r in con.execute(
                f"""SELECT head_id, ts, app_torque FROM cap_events
                    WHERE app_torque > 0 AND (app_torque < ? OR app_torque > ?)
                      AND {where}
                    ORDER BY ts""",
                [cfg.torque_min, cfg.torque_max] + params,
            ).fetchall()
        ]

    deviation_hits = []
    if method in ("deviation", "both"):
        # MEDIAN(|x - median|) per head, then flag |x - median| > k * MAD.
        deviation_hits = [
            {"head_id": int(r[0]), "ts": r[1], "app_torque": r[2],
             "reason": f"torque deviates > {cfg.mad_k}*MAD from head median {r[3]:.3f}"}
            for r in con.execute(
                f"""
                WITH caps AS (
                    SELECT head_id, ts, app_torque FROM cap_events
                    WHERE app_torque > 0 AND {where}
                ),
                med AS (
                    SELECT head_id, MEDIAN(app_torque) AS m FROM caps GROUP BY head_id
                ),
                mad AS (
                    SELECT c.head_id, m.m,
                           MEDIAN(ABS(c.app_torque - m.m)) AS mad
                    FROM caps c JOIN med m USING (head_id)
                    GROUP BY c.head_id, m.m
                )
                SELECT c.head_id, c.ts, c.app_torque, mad.m
                FROM caps c JOIN mad USING (head_id)
                WHERE mad.mad > 0
                  AND ABS(c.app_torque - mad.m) > ? * mad.mad
                ORDER BY c.ts
                """,
                params + [cfg.mad_k],
            ).fetchall()
        ]

    return ToolResult.ok(
        "anomalies",
        {
            "faults": faults,
            "threshold_hits": threshold_hits,
            "deviation_hits": deviation_hits,
            "counts": {
                "faults": len(faults),
                "threshold_hits": len(threshold_hits),
                "deviation_hits": len(deviation_hits),
            },
        },
        period=period,
        rows_scanned=len(faults) + len(threshold_hits) + len(deviation_hits),
        filters=[f"method={method}", f"band=[{cfg.torque_min}, {cfg.torque_max}]",
                 f"mad_k={cfg.mad_k}"],
        assumptions=["deviation uses median +/- k*MAD (robust); mean/sigma would let "
                     "extreme outliers inflate the band and hide themselves"],
    )
