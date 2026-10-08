"""The report structure is mandated by the brief (slide 7); it is pinned here.

The golden test is the regression net the spec asks for: a fixed store plus a
fixed plan must render byte-identical Markdown, so a change in any tool's SQL
shows up as a diff in a committed deliverable rather than as a silent shift in a
number nobody re-read.
"""
import json
from pathlib import Path

from analytics.agent.executor import Execution, execute
from analytics.agent.plan import Plan, PlanStep
from analytics.agent.router import canned_plan
from analytics.report import render
from analytics.result import ToolResult

GOLDEN = Path(__file__).parent / "fixtures" / "golden_kpi_report.md"
FIXED_TIME = "2026-07-24T12:00:00Z"


def _kpi(tiny_cfg, tmp_path):
    ex = execute(tiny_cfg, canned_plan("kpi", "2026-02"))
    narrative = render.summarise(ex)
    text = render.render(ex, tiny_cfg, tmp_path, narrative, generated_at=FIXED_TIME)
    return ex, text


def test_all_six_mandated_sections_are_present(tiny_cfg, tmp_path):
    _, text = _kpi(tiny_cfg, tmp_path)
    for heading in ("## Goal", "## Data used", "## Analyses executed",
                    "## Findings", "## Confidence and limits", "## Next checks"):
        assert heading in text, heading


def test_the_tool_call_trace_is_appended_and_machine_readable(tiny_cfg, tmp_path):
    ex, text = _kpi(tiny_cfg, tmp_path)
    assert "## Tool-call trace" in text
    trace = json.loads((tmp_path / "trace.json").read_text())
    # Steps AND the store fingerprint, read back from what render actually
    # wrote: the executor test asserts this shape but builds the dict itself,
    # so it passed while render still dumped the bare step list.
    assert trace["steps"] == ex.trace
    assert trace["store"] == ex.store
    assert trace["store"]["rows"] == 8          # the tiny fixture


def test_report_md_is_written_to_disk(tiny_cfg, tmp_path):
    _, text = _kpi(tiny_cfg, tmp_path)
    assert (tmp_path / "report.md").read_text(encoding="utf-8") == text


def test_plots_are_written_and_referenced(tiny_cfg, tmp_path):
    _, text = _kpi(tiny_cfg, tmp_path)
    for png in tmp_path.glob("*.png"):
        assert f"({png.name})" in text, f"{png.name} written but never referenced"
    assert list(tmp_path.glob("*.png")), "KPI report produced no figures at all"


def test_a_model_shaped_plan_gets_the_same_figures_as_a_router_one(tiny_cfg, tmp_path):
    """Structured outputs make the model spell out every argument, nulling the ones
    it does not set. Null means "the tool's default", so a plan that leaves `by`
    null must draw exactly the figure that a plan saying `by="head"` draws -- the
    figures cannot depend on whether the default was written out."""
    spelled = Plan(goal="g", steps=[
        PlanStep("success_rates", {"period": "2026-02", "by": "head"})])
    nulled = Plan(goal="g", source="llm", steps=[
        PlanStep("success_rates", {"period": "2026-02", "by": None, "outcome": None,
                                   "bucket": None, "method": None, "signal": None,
                                   "window": None, "min_seconds": None, "heads": None})])

    drawn = []
    for name, plan in (("spelled", spelled), ("nulled", nulled)):
        out = tmp_path / name
        out.mkdir()
        ex = execute(tiny_cfg, plan)
        render.render(ex, tiny_cfg, out, render.summarise(ex), generated_at=FIXED_TIME)
        drawn.append(sorted(p.name for p in out.glob("*.png")))

    assert drawn[0], "the spelled-out plan produced no figure, so this proves nothing"
    assert drawn[1] == drawn[0], "the model-shaped plan silently lost its figures"


def _summarise(*steps_and_results):
    """summarise() over hand-built results, for the shapes the tiny store cannot
    produce. Each argument is (PlanStep, ToolResult)."""
    steps = [s for s, _ in steps_and_results]
    results = [r for _, r in steps_and_results]
    return render.summarise(Execution(plan=Plan(goal="g", steps=steps),
                                      results=results)).findings


def test_two_trend_steps_produce_two_distinguishable_findings():
    """A drift plan trends torque and success_rate. Unlabelled, both render the
    same sentence and the reader cannot tell which signal is which."""
    quiet = ToolResult.ok("trend", {"series": [], "drift": []})
    findings = _summarise(
        (PlanStep("trend", {"signal": "torque"}), quiet),
        (PlanStep("trend", {"signal": "success_rate"}), quiet),
    )
    assert "Drift (torque)" in findings
    assert "Drift (success_rate)" in findings


def test_heads_that_all_agree_are_not_reported_as_an_odd_head_out():
    """`outliers` is every head ranked, not a filtered set. On real data every
    head correlates above 0.9999, and naming the lowest printed the
    self-defeating claim that the odd head out correlates 1.000."""
    ranked = [{"head_id": h, "mean_correlation": c}
              for h, c in ((24, 0.99995), (7, 0.99997), (3, 0.99999))]
    findings = _summarise((PlanStep("head_correlation", {}),
                           ToolResult.ok("head_correlation",
                                         {"matrix": {}, "outliers": ranked})))
    assert "Odd head out" not in findings
    assert "Head agreement" in findings
    assert "none is out of step" in findings


def test_a_head_that_really_is_out_of_step_is_still_named():
    ranked = [{"head_id": 12, "mean_correlation": 0.71},
              {"head_id": 4, "mean_correlation": 0.998}]
    findings = _summarise((PlanStep("head_correlation", {}),
                           ToolResult.ok("head_correlation",
                                         {"matrix": {}, "outliers": ranked})))
    assert "Odd head out" in findings
    assert "Head 12" in findings and "0.710" in findings


def test_closures_with_no_verdict_are_disclosed_not_silently_dropped():
    """The rate's denominator is successful + rejected. On the real store 2,927
    February closures were neither, so the two counts did not add up to the
    capping-operations figure printed two lines above and nothing said why."""
    findings = _summarise((PlanStep("success_rates", {"by": "overall"}),
                           ToolResult.ok("success_rates",
                                         {"total": 1000, "successful": 900,
                                          "failed": 40, "success_rate": 900 / 940,
                                          "lowest_head": 3})))
    assert "60 closures carry no pass/fail verdict" in findings


def test_a_rate_whose_counts_do_add_up_says_nothing_extra():
    findings = _summarise((PlanStep("success_rates", {"by": "overall"}),
                           ToolResult.ok("success_rates",
                                         {"total": 940, "successful": 900,
                                          "failed": 40, "success_rate": 900 / 940,
                                          "lowest_head": 3})))
    assert "no pass/fail verdict" not in findings


def test_provenance_reaches_the_limits_section(tiny_cfg, tmp_path):
    _, text = _kpi(tiny_cfg, tmp_path)
    limits = text.split("## Confidence and limits")[1].split("##")[0]
    assert "rows scanned" in limits.lower()
    assert "no-load cycles" in limits.lower()   # the assumption every tool carries


def test_a_failed_step_is_named_in_limits_not_hidden(tiny_cfg, tmp_path):
    from analytics.agent.plan import Plan, PlanStep
    ex = execute(tiny_cfg, Plan(goal="broken", steps=[
        PlanStep("overview", {"period": "2026-02"}, "fine"),
        PlanStep("overview", {"period": "not-a-month"}, "broken"),
    ]))
    text = render.render(ex, tiny_cfg, tmp_path, render.summarise(ex),
                         generated_at=FIXED_TIME)
    limits = text.split("## Confidence and limits")[1].split("##")[0]
    # The claim is that the offending period is NAMED, so assert the name.
    # `... or "error" in limits.lower()` made this unfailable: _limits emits
    # the literal word "error" for any failed step, which the test guarantees
    # by construction.
    assert "not-a-month" in limits


def test_router_note_is_disclosed_in_limits(tiny_cfg, tmp_path):
    from analytics.agent.router import route
    ex = execute(tiny_cfg, route("hello", "2026-02"))
    text = render.render(ex, tiny_cfg, tmp_path, render.summarise(ex),
                         generated_at=FIXED_TIME)
    assert "keyword" in text.split("## Confidence and limits")[1].lower()


def test_golden_report_is_byte_stable(tiny_cfg, tmp_path):
    # The golden is a committed fixture, never written by the test: a test that
    # blesses its own expected output cannot fail, and would silently re-bless a
    # regression the moment someone deleted the file. Regenerate deliberately:
    #   python -m tests.regen_golden      (writes the fixture, then read the diff)
    assert GOLDEN.exists(), (
        f"missing golden fixture {GOLDEN}; regenerate with "
        "`../.venv/bin/python -m tests.regen_golden` and review the diff"
    )
    _, text = _kpi(tiny_cfg, tmp_path)
    assert text == GOLDEN.read_text(encoding="utf-8"), (
        "The KPI report changed. If that is intentional, regenerate the golden "
        "with `../.venv/bin/python -m tests.regen_golden` -- then read the diff."
    )


def test_two_same_signal_steps_do_not_overwrite_each_others_figure(tiny_cfg, tmp_path):
    """plots.py filenames were constants and render._figures appended per
    step: a 12-step model plan with two trend(torque) steps silently kept
    only the second PNG under the first's name."""
    plan = Plan(
        goal="two identical trend steps",
        steps=[
            PlanStep(tool="trend", args={"period": "2026-02", "signal": "torque"},
                     rationale="first"),
            PlanStep(tool="trend", args={"period": "2026-02", "signal": "torque"},
                     rationale="second"),
        ],
        source="router",
    )
    ex = execute(tiny_cfg, plan)
    figures = render._figures(ex, str(tmp_path))
    names = [n for _, n in figures]
    assert len(names) == len(set(names)), f"figure names collide: {names}"
    for _, name in figures:
        assert (tmp_path / name).exists(), f"referenced figure {name} not written"


def _summarise_q(question, *steps_and_results):
    steps = [s for s, _ in steps_and_results]
    results = [r for _, r in steps_and_results]
    return render.summarise(Execution(
        plan=Plan(goal="g", steps=steps, question=question), results=results))


_OVERVIEW = ToolResult.ok("overview", {
    "capping_operations": 100, "successful": 99, "failed": 1,
    "no_load_cycles": 50, "heads": [1, 2], "ts_min": "a", "ts_max": "b",
    "null_torque": 0, "invalid_torque": 3, "counter_resets": 2})


def test_idle_findings_state_the_threshold_used():
    idle = ToolResult.ok("idle_periods", {
        "periods": [{"head_id": 1, "start": "a", "end": "b", "cycles": 9,
                     "duration_seconds": 7200}],
        "total_idle_seconds": 7200, "min_seconds": 3600})
    findings = _summarise((PlanStep("idle_periods", {"min_seconds": 3600}), idle))
    assert "of at least 3,600s" in findings
    assert "not machine downtime" in findings


def test_data_used_quotes_the_plans_idle_threshold_not_only_the_default(
        tiny_cfg, tmp_path):
    idle = ToolResult.ok("idle_periods", {"periods": [], "total_idle_seconds": 0,
                                          "min_seconds": 3600})
    ex = Execution(plan=Plan(goal="g", steps=[PlanStep("idle_periods",
                                                       {"min_seconds": 3600})]),
                   results=[idle], trace=[], store={})
    text = render.render(ex, tiny_cfg, tmp_path, render.summarise(ex), "t")
    assert (f"idle threshold 3600s (config default "
            f"{tiny_cfg.idle_min_seconds}s)") in text


def test_the_weakest_head_comes_with_the_median_and_the_best():
    rows = [{"head_id": h, "total": 1000, "successful": 0, "failed": 0,
             "success_rate": r}
            for h, r in ((1, 0.999867), (2, 0.999972), (3, 0.999990))]
    findings = _summarise((PlanStep("success_rates", {"by": "head"}),
                           ToolResult.ok("success_rates", rows)))
    assert "Weakest head.** 1 at 99.9867%" in findings
    assert "Median across 3 heads: 99.9972%" in findings
    assert "best: 3 at 99.9990%" in findings


def _anomaly_values(**over):
    v = {"faults": [], "threshold_hits": [], "deviation_hits": [],
         "deviation_fallbacks": {}, "capping_operations": 4000,
         "faults_by_condition": {"Bad Closure": 3, "No InTorque": 1},
         "deviation_heads_at_floor": 36, "deviation_heads": 36,
         "counts": {"faults": 4, "threshold_hits": 2, "deviation_hits": 1000}}
    v.update(over)
    return v


def test_anomalies_report_shares_conditions_and_the_noise_floor():
    s = render.summarise(Execution(
        plan=Plan(goal="g", steps=[PlanStep("anomalies", {})]),
        results=[ToolResult.ok("anomalies", _anomaly_values())]))
    assert "1,000 beyond their head's robust band (25.0000% of capping operations)" \
        in s.findings
    assert "Rejects by condition: Bad Closure 3, No InTorque 1." in s.findings
    assert "all 36 heads" in s.findings and "sensor noise" in s.findings
    # A check the data cannot support is not proposed.
    assert "supplier" not in s.next_checks
    assert "success_rates by day" in s.next_checks


def test_a_partial_floor_is_counted_not_called_noise():
    findings = _summarise((PlanStep("anomalies", {}), ToolResult.ok(
        "anomalies", _anomaly_values(deviation_heads_at_floor=5))))
    assert "on 5 of 36 heads" in findings
    assert "sensor noise" not in findings


def test_event_gaps_are_summarised_without_blaming_a_head():
    gaps = ToolResult.ok("event_gaps", {
        "gaps": [], "gap_count": 227, "total_gap_seconds": 1233360,
        "total_gap_hours": 342.6, "min_seconds": 600,
        "longest_gap": {"start": "2026-03-15 16:51:38",
                        "end": "2026-03-16 21:30:18", "duration_seconds": 103120}})
    findings = _summarise((PlanStep("event_gaps", {}), gaps))
    assert "227 gaps longer than 600s" in findings
    assert "342.6 h in total" in findings
    assert "Longest: 28.6 h, 2026-03-15 16:51:38" in findings
    assert "no head is its cause" in findings


def test_compare_periods_lists_each_bucket_and_names_the_lowest():
    def bucket(month, caps, days, rejected, rate, share):
        return {"bucket_start": f"2026-{month}-01 00:00:00", "caps": caps,
                "successful": caps - rejected, "rejected": rejected,
                "no_load_cycles": 0, "calendar_days": days, "active_days": days,
                "caps_per_day": caps / days, "caps_per_active_day": caps / days,
                "reject_rate": rate, "no_load_share": share}
    feb = bucket("02", 14824304, 28, 748, 0.00005, 0.325)
    mar = bucket("03", 3909837, 31, 204, 0.000052, 0.657)
    s = render.summarise(Execution(
        plan=Plan(goal="g", steps=[PlanStep("compare_periods", {})]),
        results=[ToolResult.ok("compare_periods", {
            "by": "month", "buckets": [feb, mar], "change_first_to_last": {},
            "lowest_volume_bucket": mar})]))
    assert "**2026-02.** 14,824,304 capping operations, 529,439/day" in s.findings
    assert "**Lowest volume.** 2026-03, at 126,124" in s.findings
    assert "event_gaps over 2026-03" in s.next_checks


def test_a_why_question_is_told_what_the_data_cannot_answer():
    s = _summarise_q("Why did the failure rate rise in April?",
                     (PlanStep("overview", {}), _OVERVIEW))
    assert s.findings.startswith("- **What this data cannot answer.**")


def test_a_what_question_gets_no_disclaimer():
    s = _summarise_q("How many caps in April?", (PlanStep("overview", {}), _OVERVIEW))
    assert "cannot answer" not in s.findings


def test_off_topic_lines_are_dropped_when_a_question_does_not_touch_them():
    s = _summarise_q("How long was the machine stopped?",
                     (PlanStep("overview", {}), _OVERVIEW))
    assert "excluded from every rate below" not in s.findings   # no rate ran
    assert "Data quality" not in s.findings
    assert "Counter resets" not in s.findings


def test_a_canned_report_keeps_every_line():
    s = _summarise_q("", (PlanStep("overview", {}), _OVERVIEW),
                     (PlanStep("success_rates", {"by": "overall"}),
                      ToolResult.ok("success_rates", {
                          "total": 100, "successful": 99, "failed": 1,
                          "success_rate": 0.99, "lowest_head": 1})))
    assert "excluded from every rate below" in s.findings
    assert "Data quality" in s.findings and "Counter resets" in s.findings


def test_limits_say_what_each_tool_that_ran_does_not_measure(tiny_cfg, tmp_path):
    from analytics.agent.registry import TOOLS
    _, text = _kpi(tiny_cfg, tmp_path)
    limits = text.split("## Confidence and limits")[1].split("## Next checks")[0]
    for tool in {s.tool for s in canned_plan("kpi", "2026-02").steps}:
        assert f"**`{tool}` does not measure.** {TOOLS[tool].not_measured}" in limits
