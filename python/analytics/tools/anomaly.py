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
from analytics.status import REJECT_SQL, decode
from analytics.store import connect, scope_clause

_METHODS = ("threshold", "deviation", "both")


def _sample(con, sql, params, limit):
    """Return (rows, exact_total) with at most `limit` rows materialised.

    Every hit used to become a Python dict inside the ToolResult, and plots.py
    then scattered all of them: 1,612,634 deviation hits on February alone, and
    the three months hold more. The counts a report quotes must stay exact, so the
    total is recomputed with COUNT(*) -- but only when the sample actually
    filled up, which on a healthy period it does not.
    """
    rows = con.execute(f"{sql} LIMIT {limit + 1}", params).fetchall()
    if len(rows) <= limit:
        return rows, len(rows)
    total = con.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
    return rows[:limit], total


def anomalies(cfg, period=None, method="both"):
    if method not in _METHODS:
        return ToolResult.error(
            "anomalies", f"method must be one of {list(_METHODS)}, got {method!r}",
            period=period,
        )

    con = connect(cfg)
    where, params = scope_clause(cfg, period)

    # rows_scanned is the provenance denominator: how many closures this tool
    # examined in scope, so "0 anomalies" is distinguishable from "no data".
    scanned = con.execute(
        f"SELECT COUNT(*) FROM cap_events WHERE {where}", params
    ).fetchone()[0]

    cap = cfg.max_anomaly_items
    fault_rows, n_faults = _sample(
        con,
        f"""SELECT head_id, ts, app_torque, status FROM cap_events
            WHERE {REJECT_SQL} AND {where} ORDER BY ts, cap_seq""",
        params, cap)
    faults = [
        {"head_id": int(r[0]), "ts": r[1], "app_torque": r[2],
         "reason": "reject: " + ", ".join(decode(r[3])["conditions"] or ["unspecified"])}
        for r in fault_rows
    ]

    threshold_hits, n_threshold = [], 0
    if method in ("threshold", "both"):
        rows, n_threshold = _sample(
            con,
            f"""SELECT head_id, ts, app_torque FROM cap_events
                WHERE app_torque > 0 AND (app_torque < ? OR app_torque > ?)
                  AND {where}
                ORDER BY ts, cap_seq""",
            [cfg.torque_min, cfg.torque_max] + params, cap)
        threshold_hits = [
            {"head_id": int(r[0]), "ts": r[1], "app_torque": r[2],
             "reason": f"torque outside band [{cfg.torque_min}, {cfg.torque_max}]"}
            for r in rows
        ]

    deviation_hits, n_deviation = [], 0
    if method in ("deviation", "both"):
        # MEDIAN(|x - median|) per head, then flag |x - median| > k * MAD.
        rows, n_deviation = _sample(
                con,
                f"""
                WITH caps AS (
                    SELECT head_id, ts, app_torque, cap_seq FROM cap_events
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
                ORDER BY c.ts, c.cap_seq
                """,
                params + [cfg.mad_k], cap)
        deviation_hits = [
            {"head_id": int(r[0]), "ts": r[1], "app_torque": r[2],
             "reason": f"torque deviates > {cfg.mad_k}*MAD from head median {r[3]:.3f}"}
            for r in rows
        ]

    return ToolResult.ok(
        "anomalies",
        {
            "faults": faults,
            "threshold_hits": threshold_hits,
            "deviation_hits": deviation_hits,
            # Exact totals, independent of how many were itemised above.
            "counts": {
                "faults": n_faults,
                "threshold_hits": n_threshold,
                "deviation_hits": n_deviation,
            },
            "listed": {
                "faults": len(faults),
                "threshold_hits": len(threshold_hits),
                "deviation_hits": len(deviation_hits),
            },
        },
        period=period,
        rows_scanned=scanned,
        filters=[f"method={method}", f"band=[{cfg.torque_min}, {cfg.torque_max}]",
                 f"mad_k={cfg.mad_k}"],
        assumptions=[
            "deviation uses median +/- k*MAD (robust); mean/sigma would let "
            "extreme outliers inflate the band and hide themselves",
            f"counts are exact; the itemised lists are capped at "
            f"{cfg.max_anomaly_items} per category (see `listed`)",
        ],
    )
