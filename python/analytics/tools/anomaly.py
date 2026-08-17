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

    # The cap binds, so the sample must be representative rather than the first
    # `limit` rows. Every one of these queries already ends in ORDER BY ts, so a
    # bare LIMIT returned the EARLIEST hits -- and plots.anomalies_over_time
    # scatters this list under the title "Flagged closures over time". On
    # February's 162,019 deviation hits a 5,000-item cap covered about 0.86 of
    # 28 days: a reader saw the deviations stop three days in.
    #
    # A fixed stride, not USING SAMPLE: the sample has to be identical on every
    # run of the same store, or two regenerations of a committed report differ
    # for no reason a reader can check.
    total = con.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
    stride = -(-total // limit)          # ceil, so the stride never under-covers
    rows = con.execute(
        f"""SELECT * EXCLUDE (_mas_rn) FROM (
                SELECT *, ROW_NUMBER() OVER () AS _mas_rn FROM ({sql})
            ) WHERE (_mas_rn - 1) % {stride} = 0 LIMIT {limit}""",
        params).fetchall()
    return rows, total


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

    deviation_hits, n_deviation, fallbacks = [], 0, {}
    if method in ("deviation", "both"):
        # MEDIAN(|x - median|) per head, then flag |x - median| > k * scale,
        # where scale is the SIGMA-CONSISTENT spread: 1.4826*MAD. Raw MAD is
        # ~0.6745*sigma under normality, so "k = 3" against raw MAD was
        # really ~2.02 sigma -- internally consistent, but a reader seeing
        # "median +/- 3*MAD" reads three sigma, and the band flagged ~10.9%
        # of February production with no calibration for the reader. The
        # floor (cfg.mad_floor, in Nm) keeps a quantised sensor's tiny-but-
        # nonzero MAD from collapsing the band to sensor noise.
        #
        # A head whose readings are more than half identical has MAD = 0 --
        # routine for a quantised sensor and guaranteed for a head stuck at
        # one value, which is the failure this detector exists to catch.
        # `WHERE mad > 0` used to drop such heads from the query entirely,
        # and the report then claimed an exact zero where the statistic was
        # undefined. Fallbacks, in order:
        #   iqr    scale = 1.4826*(IQR/2) (the same sigma-multiple: under
        #          normality IQR/2 ~ 0.6745*sigma, exactly like MAD)
        #   exact  IQR = 0 too (head hard-stuck at the median): any reading
        #          that leaves the median is a deviation
        scale_ctes = f"""
                WITH caps AS (
                    SELECT head_id, ts, app_torque, cap_seq FROM cap_events
                    WHERE app_torque > 0 AND {where}
                ),
                med AS (
                    SELECT head_id, MEDIAN(app_torque) AS m FROM caps GROUP BY head_id
                ),
                spread AS (
                    SELECT c.head_id, m.m,
                           MEDIAN(ABS(c.app_torque - m.m)) AS mad,
                           (QUANTILE_CONT(c.app_torque, 0.75)
                            - QUANTILE_CONT(c.app_torque, 0.25)) / 2.0 AS half_iqr
                    FROM caps c JOIN med m USING (head_id)
                    GROUP BY c.head_id, m.m
                ),
                scale AS (
                    SELECT head_id, m,
                           GREATEST(1.4826 * CASE WHEN mad > 0 THEN mad
                                                  ELSE half_iqr END,
                                    ?) AS s,
                           CASE WHEN mad > 0 THEN 'mad'
                                WHEN half_iqr > 0 THEN 'iqr'
                                ELSE 'exact' END AS basis
                    FROM spread
                )"""
        rows, n_deviation = _sample(
                con,
                scale_ctes + """
                SELECT c.head_id, c.ts, c.app_torque, scale.m, scale.basis
                FROM caps c JOIN scale USING (head_id)
                WHERE ABS(c.app_torque - scale.m) > ? * scale.s
                ORDER BY c.ts, c.cap_seq
                """,
                params + [cfg.mad_floor, cfg.mad_k], cap)
        reasons = {
            "mad": f"torque deviates > {cfg.mad_k} sigma-equivalents "
                   f"(1.4826*MAD) from head median {{m:.3f}}",
            "iqr": f"torque deviates > {cfg.mad_k} sigma-equivalents "
                   f"(1.4826*IQR/2) from head median {{m:.3f}} "
                   "(MAD = 0: quantised readings)",
            "exact": "torque differs from head median {m:.3f} on a head otherwise "
                     "stuck there (MAD and IQR both 0)",
        }
        deviation_hits = [
            {"head_id": int(r[0]), "ts": r[1], "app_torque": r[2],
             "reason": reasons[r[4]].format(m=r[3])}
            for r in rows
        ]
        # Disclose which heads did not get the MAD band. An empty dict is the
        # healthy case; a "0 deviations" claim over a fallback head is only
        # honest if the report can see the band was not the usual one.
        fallbacks = {
            int(h): basis
            for h, basis in con.execute(
                scale_ctes + " SELECT head_id, basis FROM scale WHERE basis <> 'mad'",
                params + [cfg.mad_floor]).fetchall()
        }

    return ToolResult.ok(
        "anomalies",
        {
            "faults": faults,
            "threshold_hits": threshold_hits,
            "deviation_hits": deviation_hits,
            # Heads whose deviation band is not the usual k*MAD one, and what it
            # is instead ("iqr" or "exact"). Empty when every head had MAD > 0.
            "deviation_fallbacks": fallbacks,
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
                 f"mad_k={cfg.mad_k}", f"mad_floor={cfg.mad_floor}"],
        assumptions=[
            "deviation uses median +/- k*(1.4826*MAD), the sigma-consistent "
            "robust band (raw MAD is ~0.6745 sigma, so k would otherwise "
            "overstate the band's width); mean/sigma would let extreme "
            "outliers inflate the band and hide themselves",
            f"the deviation scale has a floor of {cfg.mad_floor} Nm so a "
            "quantised sensor cannot collapse the band to noise",
            "a head with MAD = 0 (readings mostly identical) falls back to a "
            "half-IQR band, and to exact-median comparison when the IQR is 0 "
            "too; affected heads are listed in `deviation_fallbacks`",
            f"counts are exact; the itemised lists are capped at "
            f"{cfg.max_anomaly_items} per category (see `listed`)",
        ],
    )
