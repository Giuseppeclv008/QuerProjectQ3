"""How the chance of a rejection varies with a factor: the hour of the day, or the torque.

Two of the brief's queries are about the outcome of a closure rather than about
a head: "is there a correlation between time of day and failure probability?"
and "does higher torque correlate with higher success rate?". No other tool
computes either, and a model asked them answered from whatever was in front of
it. This one counts the rejected closures per class of the factor and says how
strong the relationship is.

Both work on the closures that carry a verdict (successful or rejected), the
denominator of `success_rates`, so the rates agree with it.

Hour of day: the reject rate of each hour, and a chi-square test of whether it
depends on the hour at all. The test is only run where every hour expects at
least five rejects and five passes, the usual condition for the chi-square
approximation; otherwise there are too few rejects to say, and `p_value` is None.

Torque: the correlation between the torque and the outcome (the point-biserial
coefficient, which is Pearson's r against 1 for a success and 0 for a reject),
and the reject rate below, inside and above the configured torque band. The
coefficient is small when rejects are rare even if the extremes are bad: in
February 2026 it is 0.03, while 70 of the 72 closures with a verdict below
1.5 Nm were rejected.
"""
import math

from analytics.result import ToolResult
from analytics.status import REJECT_SQL
from analytics.store import connect, scope_clause
from analytics.tools.overview import ASSUMPTION

FACTORS = ("hour_of_day", "torque")
MIN_EXPECTED = 5          # per cell, for the chi-square approximation


def upper_gamma_q(a, x):
    """The regularised upper incomplete gamma function Q(a, x).

    Numerical Recipes' series and continued fraction; the chi-square p-value
    for `df` degrees of freedom is Q(df / 2, chi2 / 2). Written out because the
    analytics tier depends on pandas and duckdb and not on scipy.
    """
    if x <= 0:
        return 1.0
    log_prefix = -x + a * math.log(x) - math.lgamma(a)
    if x < a + 1:                                   # series for P(a, x)
        term = total = 1.0 / a
        denom = a
        for _ in range(2000):
            denom += 1
            term *= x / denom
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
        return max(0.0, 1.0 - total * math.exp(log_prefix))
    tiny = 1e-300                                   # continued fraction for Q(a, x)
    b = x + 1 - a
    c = 1 / tiny
    d = 1 / b
    h = d
    for i in range(1, 2000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = tiny if abs(d) < tiny else d
        c = b + an / c
        c = tiny if abs(c) < tiny else c
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-15:
            break
    return min(1.0, math.exp(log_prefix) * h)


def _hour_of_day(con, cfg, where, params):
    rows = con.execute(f"""
        SELECT HOUR(ts) AS hr, COUNT(*) AS verdicts,
               COUNT(*) FILTER (WHERE {REJECT_SQL}) AS rejected
        FROM cap_events
        WHERE app_torque > 0 AND (status = ? OR {REJECT_SQL}) AND {where}
        GROUP BY 1 ORDER BY 1
    """, [cfg.success_status] + params).fetchall()
    if not rows:
        return None, 0, ""
    hours = [{"hour": int(h), "closures": int(n), "rejected": int(k),
              "reject_rate": k / n if n else None} for h, n, k in rows]
    total = sum(h["closures"] for h in hours)
    rejected = sum(h["rejected"] for h in hours)
    rate = rejected / total
    values = {"by": "hour_of_day", "hours": hours, "closures": total,
              "rejected": rejected, "reject_rate": rate,
              "chi_square": None, "degrees_of_freedom": len(hours) - 1, "p_value": None}
    ranked = [h for h in hours if h["reject_rate"] is not None]
    values["highest"] = max(ranked, key=lambda h: (h["reject_rate"], -h["hour"]))
    values["lowest"] = min(ranked, key=lambda h: (h["reject_rate"], h["hour"]))
    note = ""
    expected_ok = all(h["closures"] * rate >= MIN_EXPECTED
                      and h["closures"] * (1 - rate) >= MIN_EXPECTED for h in hours)
    if rejected == 0 or len(hours) < 2:
        note = "no rejected closure, or a single hour: there is nothing to test"
    elif not expected_ok:
        note = (f"too few rejects to test: an hour expects fewer than {MIN_EXPECTED} "
                f"rejects or passes")
    else:
        chi2 = sum((h["rejected"] - h["closures"] * rate) ** 2
                   / (h["closures"] * rate * (1 - rate)) for h in hours)
        df = len(hours) - 1
        values["chi_square"] = chi2
        values["p_value"] = upper_gamma_q(df / 2, chi2 / 2)
    return values, total, note


def _torque(con, cfg, where, params):
    zone = ("CASE WHEN app_torque < CAST(? AS REAL) THEN 'below' "
            "WHEN app_torque > CAST(? AS REAL) THEN 'above' ELSE 'inside' END")
    rows = {z: (int(n), int(k)) for z, n, k in con.execute(f"""
        SELECT {zone} AS zone, COUNT(*), COUNT(*) FILTER (WHERE {REJECT_SQL})
        FROM cap_events
        WHERE app_torque > 0 AND (status = ? OR {REJECT_SQL}) AND {where}
        GROUP BY 1
    """, [cfg.torque_min, cfg.torque_max, cfg.success_status] + params).fetchall()}
    if not rows:
        return None, 0
    zones = []
    for name in ("below", "inside", "above"):
        n, k = rows.get(name, (0, 0))
        zones.append({"zone": name, "closures": n, "rejected": k,
                      "reject_rate": k / n if n else None})
    n, r, ok_mean, rejected_mean = con.execute(f"""
        SELECT COUNT(*),
               CORR(app_torque, CASE WHEN status = ? THEN 1.0 ELSE 0.0 END),
               AVG(app_torque) FILTER (WHERE status = ?),
               AVG(app_torque) FILTER (WHERE {REJECT_SQL})
        FROM cap_events
        WHERE app_torque > 0 AND (status = ? OR {REJECT_SQL}) AND {where}
    """, [cfg.success_status] * 3 + params).fetchone()
    if r is not None and math.isnan(r):
        r = None
    return ({"by": "torque", "closures": int(n), "correlation": r,
             "mean_torque_successful": ok_mean, "mean_torque_rejected": rejected_mean,
             "band": [cfg.torque_min, cfg.torque_max], "zones": zones},
            int(n))


def failure_correlation(cfg, period=None, by="hour_of_day"):
    if by not in FACTORS:
        return ToolResult.error(
            "failure_correlation", f"by must be one of {list(FACTORS)}, got {by!r}",
            period=period)
    con = connect(cfg)
    where, params = scope_clause(cfg, period)
    if by == "hour_of_day":
        values, scanned, note = _hour_of_day(con, cfg, where, params)
        assumptions = [
            ASSUMPTION,
            "the hour is the one the store holds, with no time-zone conversion",
            f"the chi-square test needs at least {MIN_EXPECTED} expected rejects and "
            f"passes in every hour; with fewer, or with no rejects, no p-value is given",
            "the p-value is the chi-square upper tail for (hours - 1) degrees of "
            "freedom; a small one says the rate varies with the hour, not why",
        ]
    else:
        values, scanned = _torque(con, cfg, where, params)
        note = ""
        assumptions = [
            ASSUMPTION,
            "only closures with a verdict (successful or rejected) are counted, as in "
            "success_rates",
            "the correlation is Pearson's r between the torque and 1 for a success, "
            "0 for a reject; with rare rejects it is small even when the extremes are bad, "
            "which is why the reject rate below, inside and above the band is given too",
        ]
    if values is None:
        return ToolResult.insufficient(
            "failure_correlation", f"no closures with a verdict in period {period!r}",
            period=period)
    if note:
        values["note"] = note
    return ToolResult.ok(
        "failure_correlation", values, period=period, rows_scanned=scanned,
        filters=[f"by={by}"], assumptions=assumptions)
