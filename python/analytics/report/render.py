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
                line = (f"- **Weakest {noun}.** {worst[label]} at "
                        f"{worst['success_rate'] * 100:.4f}% over "
                        f"{_fmt(worst['total'])} capping operations.")
                # The weakest alone has no scale: 99.9867% reads as a problem
                # until the median beside it (99.9972%) says how far off it is.
                if len(ranked) > 1:
                    best = max(ranked, key=lambda r: r["success_rate"])
                    median = statistics.median(r["success_rate"] for r in ranked)
                    line += (f" Median across {len(ranked)} {noun}s: "
                             f"{_pct(median)}; best: {best[label]} at "
                             f"{_pct(best['success_rate'])}.")
                lines.append(line)
                checks.append(f"Inspect {label.replace('_id', '')} {worst[label]} "
                              f"mechanically before the next changeover.")
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
                lines.append(
                    f"- **Torque variability.** Head {worst['head_id']} is the most "
                    f"variable (sigma = {worst['stddev']:.4f} Nm about a median of "
                    f"{worst['median']:.3f} Nm)."
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
                             f"{measured} heads, so the deviation count mostly "
                             f"reflects sensor noise, not abnormal closures.")
            elif at_floor:
                lines.append(f"  The robust band is held at its floor on "
                             f"{at_floor} of {measured} heads.")
            if v.get("deviation_fallbacks"):
                fb = v["deviation_fallbacks"]
                lines.append(f"  (Deviation band fell back from MAD for head(s) "
                             f"{sorted(fb)}: readings mostly identical.)")
            if c["faults"]:
                checks.append("Run success_rates by day to see whether the "
                              "rejected closures cluster in time.")
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
                lines.append(
                    f"- **{fmt_start(b)}.** {_fmt(b['caps'])} capping operations, "
                    f"{per_day}/day over {b['calendar_days']} days "
                    f"({b['active_days']} active); reject rate {rate} "
                    f"({_fmt(b['rejected'])} rejects); no-load share {share_nl}.")
            low = v.get("lowest_volume_bucket")
            if low:
                lines.append(f"- **Lowest volume.** {fmt_start(low)}, at "
                             f"{_fmt(round(low['caps_per_day']))} capping "
                             f"operations per day.")
                checks.append(f"Run event_gaps over {fmt_start(low)} to see how "
                              f"much of the drop is the machine stopped.")

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
        args = effective_args(step)
        keys = []
        if result.tool == "success_rates" and args.get("by", "head") == "head":
            keys = [("success_rates", "head")]
        elif result.tool == "capping_speed":
            keys = [("capping_speed", None)]
        elif result.tool == "trend" and args.get("signal", "torque") == "torque":
            keys = [("trend", "torque"), ("trend", "drift")]
        elif result.tool == "anomalies":
            keys = [("anomalies", None)]
        for key in keys:
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
