"""Markdown is the source of truth. Everything else is an export of it.

The six sections are mandated by the brief (slide 7) and are not negotiable:

    goal -> data used -> analyses executed -> findings -> confidence/limits -> next checks

Two of them do most of the honest work. "Confidence and limits" is populated from
provenance -- rows scanned, filters, assumptions, and any step that failed -- so a
gap in the analysis is stated rather than omitted. "Tool-call trace" is the
machine-readable record of every call and argument, which is both the rubric's
"clear tool-use flow" and the first place to look when a number surprises you.
"""
import json
import logging
import os
import re
import statistics
from dataclasses import dataclass

from analytics.agent.plan import effective_args
from analytics.agent.registry import TOOLS
from analytics.report import plots

log = logging.getLogger(__name__)

_PLOTTERS = {
    ("success_rates", "head"): plots.success_rate_per_head,
    ("capping_speed", None): plots.capping_speed_over_time,
    ("trend", "torque"): plots.torque_rolling_mean,
    ("trend", "drift"): plots.drift_ranking,
    ("anomalies", None): plots.anomalies_over_time,
    ("success_rates", "day"): plots.failed_closures_per_day,
    ("torque_stats", None): plots.torque_histogram,
    ("failure_correlation", "hour_of_day"): plots.failure_by_hour,
}

# The file stem each figure is written under (plots.py appends the suffix and
# ".png"), so the names can be known before anything is drawn.
_FIGURE_STEMS = {
    ("success_rates", "head"): "success_rate_per_head",
    ("success_rates", "day"): "failed_closures_per_day",
    ("capping_speed", None): "capping_speed",
    ("trend", "torque"): "torque_rolling_mean",
    ("trend", "drift"): "drift_ranking",
    ("anomalies", None): "anomalies_over_time",
    ("torque_stats", None): "torque_histogram",
    ("failure_correlation", "hour_of_day"): "failure_by_hour",
}


@dataclass(frozen=True)
class Narrative:
    findings: str
    next_checks: str
    source: str = "template"   # "template" | "llm"
    note: str = ""             # why the template was used, if the model was tried


def _fmt(value):
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


# Questions the store has no data for. The template cannot read a question,
# but it can tell when one asks for a cause, and say so before anything else
# rather than answer a different question with confidence.
_UNANSWERABLE = re.compile(
    r"\b(why|cause[sd]?|because|operators?|shifts?|batch(es)?|lots?|"
    r"suppliers?|maintenance)\b", re.I)

# Words that make the data-quality and counter-reset lines relevant. A
# canned report (no question) always gets them.
_QUALITY = re.compile(r"\b(quality|torque|band|reset|counter|data|valid)", re.I)


def _pct(x):
    return f"{x * 100:.4f}%"


# How many times wider one bucket's torque sigma must be than another's before
# the comparison says so. Month to month the sigma moves a few percent on a
# steady machine; the 2026 store goes from 0.0213 Nm to 0.1163 (5.5x).
_SPREAD_RATIO = 2.0

# A bucket with less than this share of the period's capping operations says
# too little to be compared: the store starts on 31 January at 16:00, and its
# eight hours as "2026-01" made a sigma 37 times narrower than March's.
_MIN_BUCKET_SHARE = 0.01

# A step in torque level is named when the buckets' means are this many times
# the sigma of the steadiest bucket apart. February to April is 2.5 times, and
# the weeks of March 2026 are tens of times.
_LEVEL_STEP = 5


def _share(fraction):
    """A share as a percentage that does not round a small one to zero."""
    pct = fraction * 100
    return "0%" if pct == 0 else f"{pct:.2f}%" if pct >= 0.01 else f"{pct:.5f}%"


# The most variable head "stands out" when its sigma is this many percent above
# the median sigma of the others: head 22 in February 2026 is 17% (a head that
# logged readings at 0.002 Nm), head 9 is 1.4% (nothing).
_STANDOUT_PCT = 10.0

# How a torque_stats `outcome` reads in a sentence.
_OUTCOMES = {"successful": "successful closures", "failed": "failed closures",
             "all": "all closures"}

# A reject rate counts as having moved when it changed by at least this factor
# between the first and last bucket; the rates are around 0.005%, where a few
# percent is a handful of closures.
_RATE_STEP = 1.1


def summarise(execution):
    """A findings section written from the numbers alone, with no model involved.

    This is not a placeholder for the LLM narrative -- it is the fallback that
    keeps `arol report ...` working with no API key, and the reference the LLM
    narrative is checked against.
    """
    lines, checks = [], []
    question = execution.plan.question
    tools_run = {r.tool for r in execution.results if r.status == "ok"}
    has_rates = bool(tools_run & {"success_rates", "compare_periods"})
    topical = not question or bool(_QUALITY.search(question))
    if question and _UNANSWERABLE.search(question):
        lines.append(
            "- **What this data cannot answer.** The store holds closures, "
            "torque and status per head; it records no causes, operators, "
            "shifts, cap lots, suppliers or maintenance. The findings below say "
            "what happened and when, not why.")
    for step, result in zip(execution.plan.steps, execution.results):
        if result.status != "ok":
            continue
        v = result.values
        args = effective_args(step)
        if result.tool == "overview":
            excluded = (" are excluded from every rate below" if has_rates
                        else " were also recorded")
            lines.append(
                f"- **Scope.** {_fmt(v['capping_operations'])} capping operations "
                f"across {len(v['heads'])} heads, from {v['ts_min']} "
                f"to {v['ts_max']}. {_fmt(v['no_load_cycles'])} no-load cycles"
                f"{excluded}."
            )
            # successful and failed are counted by the tool and were never
            # printed: asked how many closures succeeded, a model answered with
            # the total of capping operations. A plan that ran success_rates
            # overall already prints them.
            overall_ran = any(
                s.tool == "success_rates"
                and effective_args(s).get("by", "head") == "overall"
                for s in execution.plan.steps)
            if not overall_ran and v.get("successful") is not None:
                undecided = (v["capping_operations"] - v["successful"]
                             - (v.get("failed") or 0))
                tail = (f", and {_fmt(undecided)} carry no pass/fail verdict"
                        if undecided else "")
                lines.append(
                    f"- **Outcomes.** {_fmt(v['successful'])} successful and "
                    f"{_fmt(v.get('failed') or 0)} rejected closures{tail}.")
            if v["invalid_torque"] and topical:
                lines.append(
                    f"- **Data quality.** {_fmt(v['invalid_torque'])} closures carry "
                    f"torque outside the configured band; {_fmt(v['null_torque'])} "
                    f"carry no torque reading at all."
                )
                checks.append("Confirm the configured torque band matches the "
                              "product currently running on the line.")
            if v["counter_resets"] and topical:
                lines.append(f"- **Counter resets.** {_fmt(v['counter_resets'])} "
                             f"reset markers in scope.")
        elif result.tool == "success_rates" and isinstance(v, dict):
            rate = v["success_rate"]
            if rate is None:
                lines.append("- **Success rate.** No pass/fail verdicts in scope.")
            else:
                line = (f"- **Success rate.** {rate * 100:.4f}% "
                        f"({_fmt(v['successful'])} successful, "
                        f"{_fmt(v['failed'])} rejected). "
                        f"Lowest head: {v['lowest_head']}.")
                # The rate's denominator is successful + rejected, not every
                # capping operation: a closure that is neither status 0 nor a
                # reject carries no verdict. Say so, or the successful and
                # rejected counts visibly fail to add up to the scope line.
                undecided = v["total"] - v["successful"] - v["failed"]
                if undecided:
                    line += (f" A further {_fmt(undecided)} closures carry no "
                             f"pass/fail verdict and are outside the rate.")
                lines.append(line)
        elif result.tool == "success_rates" and isinstance(v, list):
            ranked = [r for r in v if r.get("success_rate") is not None]
            if ranked:
                worst = min(ranked, key=lambda r: r["success_rate"])
                label = "head_id" if "head_id" in worst else "day"
                noun = label.replace("_id", "")
                rejected = ""
                if worst.get("failed"):
                    # The rate alone hides the count: 99.9781% is 90 rejects, a
                    # twelfth of the machine's, against a median head of 21.
                    all_failed = sum(r.get("failed") or 0 for r in v)
                    median_failed = statistics.median(r.get("failed") or 0 for r in v)
                    rejected = (f" ({_fmt(worst['failed'])} rejected, "
                                f"{worst['failed'] / all_failed:.1%} of all "
                                f"{_fmt(all_failed)} rejects; the median {noun} has "
                                f"{median_failed:g})")
                line = (f"- **Weakest {noun}.** {worst[label]} at "
                        f"{worst['success_rate'] * 100:.4f}% over "
                        f"{_fmt(worst['total'])} capping operations{rejected}.")
                # The weakest alone has no scale: 99.9867% reads as a problem
                # until the median beside it (99.9972%) says how far off it is.
                if len(ranked) > 1:
                    best = max(ranked, key=lambda r: r["success_rate"])
                    median = statistics.median(r["success_rate"] for r in ranked)
                    tied = sum(1 for r in ranked
                               if r["success_rate"] == best["success_rate"])
                    # Several at the top (19 days at 100%) make "the best" an
                    # accident of ordering; say how many share it instead.
                    top = (f"{tied} of {len(ranked)} {noun}s at "
                           f"{_pct(best['success_rate'])}" if tied > 1 else
                           f"best: {best[label]} at {_pct(best['success_rate'])}")
                    line += (f" Median across {len(ranked)} {noun}s: "
                             f"{_pct(median)}; {top}.")
                # The ranking by count, which the rate hides: the weakest head
                # by rate is not always the second by count, and a model read a
                # table of 36 rows and ranked them wrongly.
                by_count = sorted((r for r in v if r.get("failed")),
                                  key=lambda r: -r["failed"])[:3]
                if len(by_count) > 1:
                    line += (" Most rejects: " + ", ".join(
                        f"{noun} {r[label]} ({_fmt(r['failed'])})" for r in by_count)
                        + ".")
                lines.append(line)
                # A head can be inspected; a day cannot, and its worst rate is
                # often two rejects on a low-volume day.
                if label == "head_id":
                    checks.append(f"Inspect head {worst[label]} mechanically "
                                  f"before the next changeover.")
        elif result.tool == "capping_speed":
            bucket_count = len(v['buckets'])
            bucket_noun = "bucket" if bucket_count == 1 else "buckets"
            lines.append(
                f"- **Throughput.** {_fmt(v['mean_pieces_per_hour'])} pieces/hour, "
                f"averaged over {bucket_count} active {bucket_noun}."
            )
        elif result.tool == "idle_periods":
            hours = v["total_idle_seconds"] / 3600
            threshold = v.get("min_seconds")
            longer = f" of at least {_fmt(threshold)}s" if threshold else ""
            lines.append(
                f"- **Idle time.** {_fmt(len(v['periods']))} sustained no-load "
                f"periods{longer}, {hours:,.1f} head-hours in total. This is "
                f"heads cycling without a cap, not machine downtime."
            )
            longest = v.get("longest_period")
            if longest:
                lines[-1] += (f" Longest: {longest['duration_seconds'] / 3600:,.1f} h "
                              f"on head {longest['head_id']}, {longest['start']} to "
                              f"{longest['end']}.")
            by_head = v.get("by_head") or []
            if len(by_head) > 1:
                most, least = by_head[0], by_head[-1]
                lines[-1] += (f" Per head the totals run from "
                              f"{least['total_seconds'] / 3600:,.1f} h (head "
                              f"{least['head_id']}) to "
                              f"{most['total_seconds'] / 3600:,.1f} h (head "
                              f"{most['head_id']}).")
        elif result.tool == "trend":
            # A plan may trend more than one signal. Without naming it, two
            # trend steps produce two identical, indistinguishable findings.
            signal = args.get("signal", "torque")
            drifting = [d for d in v["drift"] if d["drifting"]]
            undetermined = [d for d in v["drift"] if d.get("insufficient")]
            if drifting:
                worst = drifting[0]
                p_txt = (f", p = {worst['p_value']:.3g}"
                         if worst.get("p_value") is not None else "")
                lines.append(
                    f"- **Drift ({signal}).** {len(drifting)} head(s) drifting; the "
                    f"strongest is head {worst['head_id']} ({worst['direction']}, "
                    f"tau = {worst['tau']:.2f}{p_txt})."
                )
                checks.append(f"Re-run {signal} drift on head {worst['head_id']} next "
                              f"month; a tau that keeps its sign is a maintenance "
                              f"trigger.")
            elif undetermined and len(undetermined) == len(v["drift"]):
                # Not "no drift": with too few time buckets the test cannot say
                # either way, and reassurance from zero data is the old defect.
                lines.append(f"- **Drift ({signal}).** Undetermined: the period has "
                             f"too few time buckets for a Mann-Kendall verdict on "
                             f"any head. Re-run over a longer period.")
            else:
                suffix = (f" ({len(undetermined)} head(s) undetermined: too few "
                          f"buckets)" if undetermined else "")
                lines.append(f"- **Drift ({signal}).** No head exceeds the "
                             f"Mann-Kendall drift threshold in this period{suffix}.")
        elif result.tool == "torque_stats" and isinstance(v, list) and v:
            worst = v[0]     # already ordered by stddev DESC NULLS LAST
            if worst["stddev"] is None:
                # NULLS LAST puts any measurable head first, so a None here
                # means no head has two closures to measure variability from.
                lines.append(
                    "- **Torque variability.** Undefined: no head has more than "
                    "one closure in scope."
                )
            else:
                over = _OUTCOMES.get(args.get("outcome", "successful"), "closures")
                # The others' range is what says whether the first stands out:
                # 0.0248 against 0.0209-0.0217 is a head, 0.0213 against
                # 0.0209-0.0212 is not.
                others = [r["stddev"] for r in v[1:] if r["stddev"] is not None]
                median_others = statistics.median(others) if others else 0
                above = (f", {(worst['stddev'] / median_others - 1) * 100:.1f}% "
                         f"above the median sigma of the other heads"
                         if median_others > 0 else "")
                rest = (f"; the other {len(others)} heads run from "
                        f"{min(others):.4f} to {max(others):.4f} Nm"
                        if others else "")
                verdict = ""
                if median_others > 0:
                    verdict = (" It stands out from the other heads."
                               if (worst["stddev"] / median_others - 1) * 100
                               >= _STANDOUT_PCT else " No head stands out.")
                lines.append(
                    f"- **Torque variability.** Head {worst['head_id']} is the most "
                    f"variable over {over} (sigma = {worst['stddev']:.4f} Nm about "
                    f"a median of {worst['median']:.3f} Nm{above}){rest}.{verdict}"
                )
        elif result.tool == "torque_stats" and isinstance(v, dict) and v:
            # The brief's own example: mean, min, max and standard deviation of
            # the successful closures. The tool returned them; nothing printed them.
            over = _OUTCOMES.get(args.get("outcome", "successful"), "closures")
            sigma = (f", sigma {v['stddev']:.4f}"
                     if v.get("stddev") is not None else "")
            lines.append(
                f"- **Torque ({over}).** {_fmt(v['n'])} closures: mean "
                f"{v['mean']:.4f} Nm, min {v['min']:.3f}, max {v['max']:.3f}, "
                f"median {v['median']:.3f}{sigma}."
            )
        elif result.tool == "head_correlation":
            # `outliers` is every head ranked by mean correlation, not a filtered
            # set, so outliers[0] is only "odd" if it is actually out of step.
            # On a healthy machine every head correlates above 0.999 and naming
            # the lowest reads as a diagnosis of a head that is behaving fine --
            # printed to 3dp it says "the odd head out correlates 1.000".
            ranked = v.get("outliers") or []
            if ranked:
                odd, closest = ranked[0], ranked[-1]
                lo, hi = odd["mean_correlation"], closest["mean_correlation"]
                if hi - lo < 0.001:      # indistinguishable at the printed precision
                    lines.append(
                        f"- **Head agreement.** All {len(ranked)} heads move together "
                        f"(mean correlation {lo:.4f}-{hi:.4f}), so none is out of step "
                        f"in shape. This says nothing about level: Pearson is "
                        f"invariant to a per-head offset, and a head running steadily "
                        f"below the others scores the same."
                    )
                    checks.append("Compare per-head median torque (torque_stats "
                                  "by head) — the correlation ranking cannot see a "
                                  "head that tracks the pack at a lower level.")
                else:
                    lines.append(
                        f"- **Odd head out.** Head {odd['head_id']} has the lowest "
                        f"mean correlation to its peers ({lo:.3f}, against "
                        f"{hi:.3f} for the closest-tracking head)."
                    )
                    checks.append(f"Compare head {odd['head_id']}'s torque trace "
                                  f"against a well-behaved head over the same period.")
        elif result.tool == "anomalies":
            c = v["counts"]
            # Name only the detectors that ran: under method="threshold" the
            # deviation count is "not computed", not a measured zero (and vice
            # versa), and printing it as 0 was a positive claim from no work.
            m = args.get("method", "both")
            caps = v.get("capping_operations")

            def share(n):
                return f" ({_pct(n / caps)} of capping operations)" if caps else ""

            parts = [f"{_fmt(c['faults'])} rejected closures"]
            if m in ("threshold", "both"):
                parts.append(f"{_fmt(c['threshold_hits'])} outside the torque band"
                             f"{share(c['threshold_hits'])}")
            if m in ("deviation", "both"):
                parts.append(f"{_fmt(c['deviation_hits'])} beyond their head's "
                             f"robust band{share(c['deviation_hits'])}")
            lines.append(f"- **Anomalies.** {', '.join(parts)}.")
            by_head = v.get("threshold_by_head") or []
            if by_head and m in ("threshold", "both"):
                top = ", ".join(f"head {h['head_id']} ({_fmt(h['count'])})"
                                for h in by_head[:3])
                lines.append(f"  Most readings outside the torque band: {top}; "
                             f"{len(by_head)} head(s) have at least one.")
            by_condition = v.get("faults_by_condition") or {}
            if by_condition:
                lines.append("  Rejects by condition: " + ", ".join(
                    f"{k} {_fmt(n)}" for k, n in
                    sorted(by_condition.items(), key=lambda kv: (-kv[1], kv[0])))
                    + ".")
            at_floor, measured = (v.get("deviation_heads_at_floor") or 0,
                                  v.get("deviation_heads") or 0)
            if measured and at_floor == measured:
                lines.append(f"  The robust band is held at its floor on all "
                             f"{measured} heads, so it is a fixed distance from "
                             f"each head's median, not the head's own spread: "
                             f"read the share against another period, since a "
                             f"step in it is a step in torque level.")
            elif at_floor:
                lines.append(f"  The robust band is held at its floor on "
                             f"{at_floor} of {measured} heads.")
            if v.get("deviation_fallbacks"):
                fb = v["deviation_fallbacks"]
                lines.append(f"  (Deviation band fell back from MAD for head(s) "
                             f"{sorted(fb)}: readings mostly identical.)")
            daily_rates_ran = any(
                s.tool == "success_rates" and effective_args(s).get("by") == "day"
                for s in execution.plan.steps)
            if c["faults"] and not daily_rates_ran:
                checks.append("Run success_rates by day to see whether the "
                              "rejected closures cluster in time.")
        elif result.tool == "closure_filter":
            f = v["filters"]
            parts = []
            if f.get("above") is not None:
                parts.append(f"torque above {f['above']:g} Nm")
            if f.get("below") is not None:
                parts.append(f"torque below {f['below']:g} Nm")
            if f.get("outcome") not in (None, "all"):
                parts.append(f"outcome {f['outcome']}")
            if f.get("head") is not None:
                parts.append(f"head {f['head']}")
            share = (f" ({_share(v['count'] / v['capping_operations'])} of "
                     f"{_fmt(v['capping_operations'])} capping operations)"
                     if v.get("capping_operations") else "")
            line = (f"- **Filtered closures.** {_fmt(v['count'])} capping operations "
                    f"with {' and '.join(parts) or 'no filter'}{share}.")
            by_head = v.get("by_head") or []
            if f.get("head") is None and len(by_head) > 1 and v["count"]:
                top = ", ".join(f"head {h['head_id']} ({_fmt(h['count'])})"
                                for h in by_head[:3])
                line += f" Most on {top}; {len(by_head)} head(s) have at least one."
            if v.get("listed"):
                line += (" All are listed in the table below."
                         if v["listed"] >= v["count"] else
                         f" The first {v['listed']} are listed in the table below.")
            lines.append(line)
        elif result.tool == "failure_correlation":
            if v.get("by") == "hour_of_day":
                hi, lo, p_value = v["highest"], v["lowest"], v.get("p_value")
                if p_value is None:
                    verdict = v.get("note") or "no test could be run"
                elif p_value < 0.05:
                    verdict = (f"the differences between hours are larger than chance "
                               f"gives (chi-square p = {p_value:.3g}); that says the "
                               f"rate varies with the hour, not why")
                else:
                    verdict = (f"the differences between hours are within what chance "
                               f"gives (chi-square p = {p_value:.3g})")
                lines.append(
                    f"- **Failure by hour of day.** The reject rate runs from "
                    f"{lo['reject_rate'] * 1e5:.2f} per 100,000 closures at "
                    f"{lo['hour']:02d}:00 to {hi['reject_rate'] * 1e5:.2f} at "
                    f"{hi['hour']:02d}:00 (overall {v['reject_rate'] * 1e5:.2f}); "
                    f"{verdict}.")
            elif v.get("by") == "torque":
                r = v.get("correlation")
                if r is None:
                    corr = "The correlation between torque and success is undefined"
                else:
                    size = abs(r)
                    strength = ("negligible" if size < 0.1 else "weak" if size < 0.3
                                else "moderate" if size < 0.5 else "strong")
                    corr = (f"The correlation between torque and success is "
                            f"r = {r:+.3f} ({strength}; {_fmt(v['closures'])} closures "
                            f"with a verdict)")
                means = ""
                if v.get("mean_torque_successful") is not None \
                        and v.get("mean_torque_rejected") is not None:
                    means = (f"; mean torque {v['mean_torque_successful']:.4f} Nm for "
                             f"successful and {v['mean_torque_rejected']:.4f} Nm for "
                             f"rejected closures")
                lo_band, hi_band = v["band"]
                where = {"below": f"below {lo_band:g} Nm", "inside": "inside the band",
                         "above": f"above {hi_band:g} Nm"}
                zones = ", ".join(
                    f"{where[z['zone']]} {_fmt(z['rejected'])} of {_fmt(z['closures'])} "
                    f"rejected" + (f" ({_share(z['reject_rate'])})"
                                   if z["reject_rate"] is not None else "")
                    for z in v["zones"])
                lines.append(f"- **Failure and torque.** {corr}{means}. By the band "
                             f"{lo_band:g}-{hi_band:g} Nm: {zones}.")
        elif result.tool == "methodology":
            for item in v["topics"]:
                lines.append(f"- **Method ({item['topic']}).** {item['text']}")
        elif result.tool == "event_gaps":
            longest = v.get("longest_gap")
            line = (f"- **Stops.** {_fmt(v['gap_count'])} gaps longer than "
                    f"{_fmt(v['min_seconds'])}s with no event from any head, "
                    f"{v['total_gap_hours']:,.1f} h in total.")
            if longest:
                line += (f" Longest: {longest['duration_seconds'] / 3600:,.1f} h, "
                         f"{longest['start']} to {longest['end']}.")
            line += (" A gap is the machine stopped or its data missing; the "
                     "store cannot tell which, and no head is its cause.")
            lines.append(line)
            if longest:
                checks.append(f"Compare the longest gap ({longest['start']} to "
                              f"{longest['end']}) with the plant's stop log, or "
                              f"check the data feed for that stretch: the store "
                              f"cannot tell a stop from missing data.")
        elif result.tool == "compare_periods":
            fmt_start = (lambda b: str(b["bucket_start"])[:7] if v["by"] == "month"
                         else f"week of {str(b['bucket_start'])[:10]}")
            for b in v["buckets"]:
                rate = (_pct(b["reject_rate"]) if b["reject_rate"] is not None
                        else "n/a")
                share_nl = (f"{b['no_load_share'] * 100:.1f}%"
                            if b["no_load_share"] is not None else "n/a")
                per_day = (_fmt(round(b["caps_per_day"]))
                           if b["caps_per_day"] is not None else "n/a")
                torque = ""
                if b.get("torque_mean") is not None:
                    torque = f"; torque {b['torque_mean']:.4f} Nm"
                    if b.get("torque_stddev") is not None:
                        torque += f" (sigma {b['torque_stddev']:.4f})"
                lines.append(
                    f"- **{fmt_start(b)}.** {_fmt(b['caps'])} capping operations, "
                    f"{per_day}/day over {b['calendar_days']} days "
                    f"({b['active_days']} active); reject rate {rate} "
                    f"({_fmt(b['rejected'])} rejects); no-load share "
                    f"{share_nl}{torque}.")
            total_caps = sum(b["caps"] for b in v["buckets"])
            material = [b for b in v["buckets"]
                        if b["caps"] >= _MIN_BUCKET_SHARE * total_caps]
            rated = [b for b in material if b.get("reject_rate") is not None]
            if len(rated) > 1:
                first, last = rated[0], rated[-1]
                a, b = first["reject_rate"], last["reject_rate"]
                if a or b:
                    if a and b / a <= 1 / _RATE_STEP:
                        verb = "fell"
                    elif not a or b / a >= _RATE_STEP:
                        verb = "rose"
                    else:
                        verb = "was steady"
                    lines.append(
                        f"- **Reject rate.** {verb.capitalize()}: {_pct(a)} in "
                        f"{fmt_start(first)}, {_pct(b)} in {fmt_start(last)}.")
            low = v.get("lowest_volume_bucket")
            if low:
                lines.append(f"- **Lowest volume.** {fmt_start(low)}, at "
                             f"{_fmt(round(low['caps_per_day']))} capping "
                             f"operations per day.")
                checks.append(f"Run event_gaps over {fmt_start(low)} to see how "
                              f"much of the drop is the machine stopped.")
            # A step in torque level or spread is invisible to a drift test,
            # which reads only a steady trend; say it when the buckets differ.
            spread = [b for b in material if b.get("torque_stddev")]
            if len(spread) > 1:
                narrow = min(spread, key=lambda b: b["torque_stddev"])
                wide = max(spread, key=lambda b: b["torque_stddev"])
                ratio = wide["torque_stddev"] / narrow["torque_stddev"]
                if ratio >= _SPREAD_RATIO:
                    lines.append(
                        f"- **Torque spread.** Sigma is {ratio:.1f}x wider in "
                        f"{fmt_start(wide)} ({wide['torque_stddev']:.4f} Nm, mean "
                        f"{wide['torque_mean']:.4f}) than in {fmt_start(narrow)} "
                        f"({narrow['torque_stddev']:.4f} Nm, mean "
                        f"{narrow['torque_mean']:.4f}). A drift test reads a "
                        f"steady trend and does not see a step like this.")
                    checks.append(
                        f"Check whether a different product or setting ran in "
                        f"{fmt_start(wide)}: the torque spread changed "
                        f"{ratio:.0f}-fold, and a configured band fits one "
                        f"product only.")
            levels = [b for b in material
                      if b.get("torque_mean") is not None and b.get("torque_stddev")]
            if len(levels) > 1:
                low = min(levels, key=lambda b: b["torque_mean"])
                high = max(levels, key=lambda b: b["torque_mean"])
                steady = min(b["torque_stddev"] for b in levels)
                gap = high["torque_mean"] - low["torque_mean"]
                if gap >= _LEVEL_STEP * steady:
                    lines.append(
                        f"- **Torque level.** The mean moves from "
                        f"{low['torque_mean']:.4f} Nm in {fmt_start(low)} to "
                        f"{high['torque_mean']:.4f} Nm in {fmt_start(high)}: "
                        f"{gap:.4f} Nm apart, {gap / steady:.0f} times the sigma of "
                        f"the steadiest bucket ({steady:.4f} Nm). A drift test "
                        f"reads a steady trend and does not see a step in level.")
                    checks.append(
                        f"Check whether a different product or setting ran "
                        f"between {fmt_start(low)} and {fmt_start(high)}: the mean "
                        f"torque moved {gap:.2f} Nm.")

    if not lines:
        lines.append("- No analysis in this plan returned usable data. "
                     "See *Confidence and limits* below.")
    if not checks:
        checks.append("Re-run this report next period and compare the numbers.")
    # Two steps of one tool can propose the same check; say it once.
    checks = list(dict.fromkeys(checks))
    return Narrative(
        findings="\n".join(lines),
        next_checks="\n".join(f"- {c}" for c in checks),
        source="template",
    )


def tables(execution, max_rows=None, max_day_rows=None):
    """Markdown tables for the results that are one row per head or per day.

    The findings name the weakest head or day; the table is every one, which
    is what "the success rate of each head", "a daily breakdown" or "compare
    head 1 and head 2" ask for. Built from the ToolResults alone. `max_rows`
    cuts a long table and says so, and `max_day_rows` the per-day ones apart
    (they are the long ones); the report prints them whole.
    """
    blocks = []
    for step, result in zip(execution.plan.steps, execution.results):
        v = result.values
        if result.status != "ok" or not v:
            continue
        args = effective_args(step)
        if result.tool == "closure_filter" and isinstance(v, dict) and v.get("events"):
            title = "Matching closures"
            header = ["Head", "Time", "Torque (Nm)", "Status", "Outcome"]
            rows = [[str(e["head_id"]), str(e["ts"]), f"{e['app_torque']:.3f}",
                     f"{e['status']:g}", e["outcome"]] for e in v["events"]]
        elif result.tool == "failure_correlation" and isinstance(v, dict) and v.get("by") == "hour_of_day":
            title = "Failure by hour of day"
            header = ["Hour", "Closures", "Rejected", "Rejects per 100,000"]
            rows = [[f"{h['hour']:02d}:00", _fmt(h["closures"]), _fmt(h["rejected"]),
                     "n/a" if h["reject_rate"] is None else f"{h['reject_rate'] * 1e5:.2f}"]
                    for h in v["hours"]]
        elif result.tool == "failure_correlation" and isinstance(v, dict) and v.get("by") == "torque":
            lo_band, hi_band = v["band"]
            names = {"below": f"below {lo_band:g} Nm", "inside": f"{lo_band:g}-{hi_band:g} Nm",
                     "above": f"above {hi_band:g} Nm"}
            title = "Failure by torque zone"
            header = ["Torque", "Closures", "Rejected", "Reject rate"]
            rows = [[names[z["zone"]], _fmt(z["closures"]), _fmt(z["rejected"]),
                     "n/a" if z["reject_rate"] is None else _share(z["reject_rate"])]
                    for z in v["zones"]]
        elif not isinstance(v, list):
            continue
        elif result.tool == "success_rates":
            by_head = "head_id" in v[0]
            key = "head_id" if by_head else "day"
            title = f"Success rate per {'head' if by_head else 'day'}"
            header = ["Head" if by_head else "Day", "Closures", "Successful",
                      "Rejected", "Success rate"]
            rows = [[str(r[key])[:10], _fmt(r["total"]), _fmt(r["successful"]),
                     _fmt(r["failed"]),
                     "n/a" if r.get("success_rate") is None else _pct(r["success_rate"])]
                    for r in v]
        elif result.tool == "torque_stats" and "head_id" in v[0]:
            over = _OUTCOMES.get(args.get("outcome", "successful"), "closures")
            title = f"Torque per head ({over})"
            header = ["Head", "Closures", "Mean (Nm)", "Min", "Max", "Sigma", "Median"]
            rows = [[str(r["head_id"]), _fmt(r["n"]), f"{r['mean']:.4f}",
                     f"{r['min']:.3f}", f"{r['max']:.3f}",
                     "n/a" if r["stddev"] is None else f"{r['stddev']:.4f}",
                     f"{r['median']:.3f}"] for r in v]
        else:
            continue
        cap = (max_day_rows if max_day_rows is not None and header[0] == "Day"
               else max_rows)
        shown = rows if cap is None else rows[:cap]
        out = [f"### {title} (table)", "", "| " + " | ".join(header) + " |",
               "|" + "|".join("---" for _ in header) + "|"]
        out += ["| " + " | ".join(row) + " |" for row in shown]
        if len(shown) < len(rows):
            out += ["", f"*{len(rows) - len(shown)} more rows not shown.*"]
        blocks.append("\n".join(out))
    return "\n\n".join(blocks)


def _figure_keys(result, args):
    """The figures a result supports, as keys of _PLOTTERS."""
    if result.tool == "success_rates":
        by = args.get("by", "head")
        return [("success_rates", by)] if by in ("head", "day") else []
    if result.tool == "capping_speed":
        return [("capping_speed", None)]
    if result.tool == "trend" and args.get("signal", "torque") == "torque":
        return [("trend", "torque"), ("trend", "drift")]
    if result.tool == "anomalies":
        return [("anomalies", None)]
    if result.tool == "torque_stats" and not args.get("by"):
        return [("torque_stats", None)]
    if result.tool == "failure_correlation" and args.get("by", "hour_of_day") == "hour_of_day":
        return [("failure_correlation", "hour_of_day")]
    return []


def planned_figures(execution):
    """(caption, filename) of the figures `render` will draw, without drawing.

    A model asked to "plot" or "chart" something has to know whether the report
    draws it. Mirrors `_figures`, including the step suffix a repeated figure
    gets, and lists only the results a figure can come from.
    """
    out, seen = [], set()
    for index, (step, result) in enumerate(
            zip(execution.plan.steps, execution.results), start=1):
        for key in _figure_keys(result, effective_args(step)):
            suffix = "" if key not in seen else f"_step{index}"
            seen.add(key)
            if result.status == "ok" and result.values:
                stem = _FIGURE_STEMS[key]
                name = f"{stem}{suffix}.png"
                out.append((name.replace("_", " ").replace(".png", ""), name))
    return out


def _figures(execution, out_dir):
    """Draw whatever the results support. Returns [(caption, filename), ...]."""
    figures = []
    seen = set()
    for index, (step, result) in enumerate(
            zip(execution.plan.steps, execution.results), start=1):
        # Read the step the way the executor did: a null argument means the tool's
        # own default was used, so that default is what decides the figure. A model
        # plan spells out every argument, nulling the ones it does not set, and
        # matching on the raw args would silently drop every figure from every
        # model-planned report.
        for key in _figure_keys(result, effective_args(step)):
            # Plot filenames are constants and the 12-step plan tier has no
            # dedup, so two same-signal steps would overwrite each other's PNG.
            # First writer keeps the plain name; repeats get a step suffix, so
            # every figure the report references exists.
            suffix = "" if key not in seen else f"_step{index}"
            seen.add(key)
            name = _PLOTTERS[key](result, out_dir, suffix)
            if name:
                figures.append((name.replace("_", " ").replace(".png", ""), name))
    return figures


def _limits(execution, narrative=None):
    lines = []
    if execution.plan.note:
        lines.append(f"- **Planning.** {execution.plan.note}.")
    if narrative is not None and narrative.note:
        lines.append(f"- **Narration.** {narrative.note}.")
    if execution.plan.source == "router":
        lines.append("- **No model was used to plan this report.** The tool calls "
                     "below are a fixed plan; the numbers would be identical either way.")
    for result in execution.results:
        p = result.provenance
        if result.status == "ok":
            lines.append(
                f"- `{result.tool}`: {p.rows_scanned:,} rows scanned"
                + (f"; filters: {', '.join(p.filters)}" if p.filters else "")
            )
        else:
            lines.append(f"- `{result.tool}`: **{result.status}** — {result.message}")
    for tool in dict.fromkeys(r.tool for r in execution.results):
        spec = TOOLS.get(tool)
        if spec is not None and spec.not_measured:
            lines.append(f"- **`{tool}` does not measure.** {spec.not_measured}")
    assumptions = sorted({a for r in execution.results for a in r.provenance.assumptions})
    for a in assumptions:
        lines.append(f"- **Assumption.** {a}.")
    return "\n".join(lines)


def _model_line(cfg, execution, narrative):
    """Who actually wrote the words. In a project whose thesis is that every
    number carries its provenance, the model must not be the one contributor
    without it: a mistyped --model falls back to the keyword router, and the
    report has to say so rather than quietly claiming a model wrote it."""
    if narrative.source == "llm" or execution.plan.source == "llm":
        return f"{cfg.provider}:{cfg.model}"
    return "none (deterministic template and router)"


def render(execution, cfg, out_dir, narrative, generated_at):
    """Write report.md, trace.json, and the figures. Returns the Markdown."""
    model_line = _model_line(cfg, execution, narrative)
    out_dir = str(out_dir)
    figures = _figures(execution, out_dir)

    analyses = "\n".join(
        f"{t['step']}. `{t['tool']}({', '.join(f'{k}={v!r}' for k, v in sorted(t['args'].items()))})`"
        f" — {t['rationale']} → **{t['status']}**"
        for t in execution.trace
    )
    fp = execution.store or {}
    fingerprint = (
        f"- Store fingerprint: {fp['rows']:,} rows, {fp['distinct_heads']} heads, "
        f"{fp['ts_min']} → {fp['ts_max']}"
        if fp and "rows" in fp
        else f"- Store fingerprint: unavailable ({fp.get('error', 'not recorded')})"
    )
    # The thresholds a step actually used, not just the config's: a plan can
    # set min_seconds itself, and a "Data used" line quoting the default then
    # describes a different analysis from the one that ran.
    def used(tool):
        return sorted({r.values["min_seconds"] for r in execution.results
                       if r.tool == tool and r.status == "ok"
                       and isinstance(r.values, dict) and "min_seconds" in r.values})

    idle_used = used("idle_periods")
    idle_txt = (f"idle threshold {', '.join(f'{t}s' for t in idle_used)} "
                f"(config default {cfg.idle_min_seconds}s)"
                if idle_used and idle_used != [cfg.idle_min_seconds]
                else f"idle threshold {cfg.idle_min_seconds}s")
    gaps_used = used("event_gaps")
    if gaps_used:
        idle_txt += f"; stop gaps longer than {', '.join(f'{t}s' for t in gaps_used)}"
    data_used = "\n".join([
        f"- Store: `{os.path.basename(cfg.store_path)}`, machine `{cfg.machine_id}`",
        fingerprint,
        f"- Torque band: {cfg.torque_min}–{cfg.torque_max} Nm; "
        f"robust band k = {cfg.mad_k}; {idle_txt}",
        f"- Rows scanned across all steps: "
        f"{sum(r.provenance.rows_scanned for r in execution.results):,}",
    ])
    figure_block = "\n\n".join(
        f"### {caption.title()}\n\n![{caption}]({name})" for caption, name in figures
    )
    figure_block = "\n\n".join(x for x in (tables(execution), figure_block) if x)

    text = f"""# {execution.plan.goal}

*Generated {generated_at} — narrative source: {narrative.source}, plan source: {execution.plan.source}, model: {model_line}.*

## Goal

{execution.plan.goal}

## Data used

{data_used}

## Analyses executed

{analyses}

## Findings

{narrative.findings}

{figure_block}

## Confidence and limits

{_limits(execution, narrative)}

## Next checks

{narrative.next_checks}

## Tool-call trace

Every call this report is built from, in order. The full record is in
`trace.json` alongside this file.

| # | tool | arguments | status | rows scanned |
|---|---|---|---|---|
""" + "\n".join(
        f"| {t['step']} | `{t['tool']}` | "
        f"`{', '.join(f'{k}={v!r}' for k, v in sorted(t['args'].items())) or '—'}` | "
        f"{t['status']} | {t['rows_scanned']:,} |"
        for t in execution.trace
    ) + "\n"

    with open(out_dir + "/report.md", "w", encoding="utf-8") as fh:
        fh.write(text)
    # The store fingerprint travels with the steps: a trace that names its tool
    # calls but not the data they ran against is the archaeology
    # store_fingerprint() exists to end. This is the shape the executor's
    # round-trip test asserts.
    with open(out_dir + "/trace.json", "w", encoding="utf-8") as fh:
        json.dump({"store": execution.store, "steps": execution.trace},
                  fh, indent=2, default=str)
    log.info("wrote %s/report.md (%d figures)", out_dir, len(figures))
    return text
