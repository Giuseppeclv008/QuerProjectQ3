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
from dataclasses import dataclass

from analytics.agent.plan import effective_args
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


def _fmt(value):
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def summarise(execution):
    """A findings section written from the numbers alone, with no model involved.

    This is not a placeholder for the LLM narrative -- it is the fallback that
    keeps `arol report ...` working with no API key, and the reference the LLM
    narrative is checked against.
    """
    lines, checks = [], []
    for step, result in zip(execution.plan.steps, execution.results):
        if result.status != "ok":
            continue
        v = result.values
        args = effective_args(step)
        if result.tool == "overview":
            lines.append(
                f"- **Scope.** {_fmt(v['capping_operations'])} capping operations "
                f"across heads {v['heads'][0]}-{v['heads'][-1]}, from {v['ts_min']} "
                f"to {v['ts_max']}. {_fmt(v['no_load_cycles'])} no-load cycles are "
                f"excluded from every rate below."
            )
            if v["invalid_torque"]:
                lines.append(
                    f"- **Data quality.** {_fmt(v['invalid_torque'])} closures carry "
                    f"torque outside the configured band; {_fmt(v['null_torque'])} "
                    f"carry no torque reading at all."
                )
                checks.append("Confirm the configured torque band matches the "
                              "product currently running on the line.")
            if v["counter_resets"]:
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
                lines.append(
                    f"- **Weakest {label.replace('_id', '')}.** {worst[label]} at "
                    f"{worst['success_rate'] * 100:.4f}% over {_fmt(worst['total'])} "
                    f"capping operations."
                )
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
            lines.append(
                f"- **Idle time.** {len(v['periods'])} sustained no-load periods, "
                f"{hours:,.1f} head-hours in total."
            )
        elif result.tool == "trend":
            # A plan may trend more than one signal. Without naming it, two
            # trend steps produce two identical, indistinguishable findings.
            signal = args.get("signal", "torque")
            drifting = [d for d in v["drift"] if d["drifting"]]
            if drifting:
                worst = drifting[0]
                lines.append(
                    f"- **Drift ({signal}).** {len(drifting)} head(s) drifting; the "
                    f"strongest is head {worst['head_id']} ({worst['direction']}, "
                    f"tau = {worst['tau']:.2f})."
                )
                checks.append(f"Re-run {signal} drift on head {worst['head_id']} next "
                              f"month; a tau that keeps its sign is a maintenance "
                              f"trigger.")
            else:
                lines.append(f"- **Drift ({signal}).** No head exceeds the "
                             f"Mann-Kendall drift threshold in this period.")
        elif result.tool == "torque_stats" and isinstance(v, list) and v:
            worst = v[0]     # already ordered by stddev DESC
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
                        f"- **Head agreement.** All {len(ranked)} heads track each "
                        f"other closely (mean correlation {lo:.4f}-{hi:.4f}); none "
                        f"is out of step."
                    )
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
            lines.append(
                f"- **Anomalies.** {_fmt(c['faults'])} rejected closures, "
                f"{_fmt(c['threshold_hits'])} outside the torque band, "
                f"{_fmt(c['deviation_hits'])} beyond their head's robust band."
            )
            if c["faults"]:
                checks.append("Correlate the rejected closures against the cap "
                              "supplier lot running at those timestamps.")

    if not lines:
        lines.append("- No analysis in this plan returned usable data. "
                     "See *Confidence and limits* below.")
    if not checks:
        checks.append("Re-run this report next period and compare the numbers.")
    return Narrative(
        findings="\n".join(lines),
        next_checks="\n".join(f"- {c}" for c in checks),
        source="template",
    )


def _figures(execution, out_dir):
    """Draw whatever the results support. Returns [(caption, filename), ...]."""
    figures = []
    for step, result in zip(execution.plan.steps, execution.results):
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
            name = _PLOTTERS[key](result, out_dir)
            if name:
                figures.append((name.replace("_", " ").replace(".png", ""), name))
    return figures


def _limits(execution):
    lines = []
    if execution.plan.note:
        lines.append(f"- **Planning.** {execution.plan.note}.")
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
    assumptions = sorted({a for r in execution.results for a in r.provenance.assumptions})
    for a in assumptions:
        lines.append(f"- **Assumption.** {a}.")
    return "\n".join(lines)


def render(execution, cfg, out_dir, narrative, generated_at):
    """Write report.md, trace.json, and the figures. Returns the Markdown."""
    out_dir = str(out_dir)
    figures = _figures(execution, out_dir)

    analyses = "\n".join(
        f"{t['step']}. `{t['tool']}({', '.join(f'{k}={v!r}' for k, v in sorted(t['args'].items()))})`"
        f" — {t['rationale']} → **{t['status']}**"
        for t in execution.trace
    )
    data_used = "\n".join([
        f"- Store: `{os.path.basename(cfg.store_path)}`, machine `{cfg.machine_id}`",
        f"- Torque band: {cfg.torque_min}–{cfg.torque_max} Nm; "
        f"robust band k = {cfg.mad_k}; idle threshold {cfg.idle_min_seconds}s",
        f"- Rows scanned across all steps: "
        f"{sum(r.provenance.rows_scanned for r in execution.results):,}",
    ])
    figure_block = "\n\n".join(
        f"### {caption.title()}\n\n![{caption}]({name})" for caption, name in figures
    )

    text = f"""# {execution.plan.goal}

*Generated {generated_at} — narrative source: {narrative.source}, plan source: {execution.plan.source}.*

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

{_limits(execution)}

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
    with open(out_dir + "/trace.json", "w", encoding="utf-8") as fh:
        json.dump(execution.trace, fh, indent=2, default=str)
    log.info("wrote %s/report.md (%d figures)", out_dir, len(figures))
    return text
