"""Side-by-side periods: did the machine get better or worse?

trend answers "is a series drifting" per head; this answers the plainer
question an operator asks -- this month against that one -- for the machine as
a whole: how much it capped, how often it rejected, how much it cycled without
a cap, and on how many days it worked at all.

Volume is given two ways, because they answer different questions. Per
calendar day (the bucket's days inside the period) shows what the month
delivered: March 2026 averages ~126k caps/day against ~529k in February. Per
ACTIVE day (a day with at least one capping operation) shows how hard the
machine ran when it ran (~150k in March), and active_days sits beside both so
a month that worked on few days is visible rather than averaged away. The
reject rate uses the success_rates denominator (successful + rejected), so the
two tools agree.

Torque comes with the volume: mean, median and sigma of the bucket's capping
operations. trend reads a monotone drift per head and cannot see a step, and
the three months of 2026 hold one: mean 1.9996 Nm in February, 2.0531 in
March and 2.0203 in April, with sigma 0.0213, 0.1163 and 0.0861. A first-to-last
comparison of the mean would have missed it.
"""
from datetime import date, datetime, timedelta

from analytics.result import ToolResult
from analytics.status import REJECT_SQL
from analytics.store import connect, period_clause, scope_clause
from analytics.tools.overview import ASSUMPTION

_BUCKETS = {"month": "MONTH", "week": "WEEK"}


def _ratio(num, den):
    return num / den if den else None


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _next_start(start, by):
    if by == "week":
        return start + timedelta(days=7)
    return date(start.year + start.month // 12, start.month % 12 + 1, 1)


def _calendar_days(bucket_start, by, lo, hi):
    """Days of the bucket inside [lo, hi): a month is clipped to the period."""
    start = _as_date(bucket_start)
    end = _next_start(start, by)
    return max((min(end, hi) - max(start, lo)).days, 0)


def compare_periods(cfg, period=None, by="month"):
    if by not in _BUCKETS:
        return ToolResult.error(
            "compare_periods", f"by must be one of {sorted(_BUCKETS)}, got {by!r}",
            period=period)
    unit = _BUCKETS[by]
    con = connect(cfg)
    where, params = scope_clause(cfg, period)

    rows = con.execute(f"""
        SELECT DATE_TRUNC('{unit}', ts) AS bucket_start,
               COUNT(*)                                               AS events,
               COUNT(*) FILTER (WHERE app_torque > 0)                 AS caps,
               COUNT(*) FILTER (WHERE status = ? AND app_torque > 0)  AS successful,
               COUNT(*) FILTER (WHERE {REJECT_SQL} AND app_torque > 0) AS rejected,
               COUNT(*) FILTER (WHERE status = ? AND app_torque = 0)  AS no_load,
               COUNT(DISTINCT CAST(ts AS DATE))
                   FILTER (WHERE app_torque > 0)                      AS active_days,
               AVG(app_torque) FILTER (WHERE app_torque > 0)          AS torque_mean,
               MEDIAN(app_torque) FILTER (WHERE app_torque > 0)       AS torque_median,
               STDDEV_SAMP(app_torque) FILTER (WHERE app_torque > 0)  AS torque_stddev
        FROM cap_events
        WHERE {where}
        GROUP BY 1 ORDER BY 1
    """, [cfg.success_status, cfg.no_load_status] + params).fetchall()

    if not rows:
        return ToolResult.insufficient(
            "compare_periods", f"no events in period {period!r}", period=period)

    # The window the calendar days are clipped to: the period, or for the
    # whole store the days the data spans.
    _, bounds = period_clause(period)
    if bounds:
        lo, hi = (date.fromisoformat(b) for b in bounds)
    else:
        first_ts, last_ts = con.execute(
            f"SELECT MIN(ts), MAX(ts) FROM cap_events WHERE {where}",
            params).fetchone()
        lo, hi = _as_date(first_ts), _as_date(last_ts) + timedelta(days=1)

    buckets = []
    for r in rows:
        days = _calendar_days(r[0], by, lo, hi)
        buckets.append({
         "bucket_start": r[0], "caps": r[2], "successful": r[3],
         "rejected": r[4], "no_load_cycles": r[5],
         "calendar_days": days, "active_days": r[6],
         "torque_mean": r[7], "torque_median": r[8], "torque_stddev": r[9],
         "caps_per_day": _ratio(r[2], days),
         "caps_per_active_day": _ratio(r[2], r[6]),
         "reject_rate": _ratio(r[4], r[3] + r[4]),
         "no_load_share": _ratio(r[5], r[2] + r[5])})
    change = None
    if len(buckets) > 1:
        first, last = buckets[0], buckets[-1]

        def diff(key):
            a, b = first[key], last[key]
            return None if a is None or b is None else b - a

        change = {
            "from": first["bucket_start"], "to": last["bucket_start"],
            "caps_per_day": diff("caps_per_day"),
            "caps_per_day_pct":
                (None if not first["caps_per_day"] or last["caps_per_day"] is None
                 else 100.0 * diff("caps_per_day") / first["caps_per_day"]),
            "caps_per_active_day": diff("caps_per_active_day"),
            "reject_rate": diff("reject_rate"),
            "no_load_share": diff("no_load_share"),
            "active_days": diff("active_days"),
            "torque_mean": diff("torque_mean"),
            "torque_stddev": diff("torque_stddev"),
        }
    # First-to-last hides a dip in the middle (February to April looks like
    # -19%; March alone fell by three quarters), so the weakest bucket is
    # named outright.
    rated = [b for b in buckets if b["caps_per_day"] is not None]
    lowest = min(rated, key=lambda b: b["caps_per_day"]) if len(rated) > 1 else None
    return ToolResult.ok(
        "compare_periods",
        {"by": by, "buckets": buckets, "change_first_to_last": change,
         "lowest_volume_bucket": lowest},
        period=period,
        rows_scanned=sum(r[1] for r in rows),
        filters=[f"by={by}"],
        assumptions=[
            ASSUMPTION,
            "caps per day divides by the bucket's calendar days inside the "
            "period; caps per active day divides by days with at least one "
            "capping operation",
            "reject rate = rejected / (successful + rejected), the success_rates "
            "denominator; no-load share = no-load cycles / (capping operations + "
            "no-load cycles)",
            "a bucket is a calendar month or ISO week clipped to the period, so "
            "the first and last may be partial",
            "torque level and spread are over the bucket's capping operations "
            "(torque > 0), all heads together; a step in either can be a change "
            "of product or setting as well as a change in the machine",
        ],
    )
