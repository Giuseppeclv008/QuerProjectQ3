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
    assert "all 36 heads" in s.findings
    # A band held at its floor is a fixed distance from the median. It is not
    # evidence of noise: the same count was 1.1% of caps in a steady month and
    # 20.4% where torque stepped, so the report must not call it noise.
    assert "a fixed distance from each head's median" in s.findings
    assert "sensor noise" not in s.findings
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


def _torque_buckets(*specs):
    """compare_periods buckets from (month, mean torque, sigma), the volume
    fields filled in with steady values."""
    return [{"bucket_start": f"2026-{m}-01 00:00:00", "caps": 1000,
             "successful": 1000, "rejected": 0, "no_load_cycles": 0,
             "calendar_days": 30, "active_days": 30,
             "caps_per_day": 1000 / 30, "caps_per_active_day": 1000 / 30,
             "reject_rate": 0.0, "no_load_share": 0.0,
             "torque_mean": mean, "torque_median": mean, "torque_stddev": sd}
            for m, mean, sd in specs]


def _compare(buckets):
    return render.summarise(Execution(
        plan=Plan(goal="g", steps=[PlanStep("compare_periods", {})]),
        results=[ToolResult.ok("compare_periods", {
            "by": "month", "buckets": buckets, "change_first_to_last": {},
            "lowest_volume_bucket": None})]))


def test_each_month_line_carries_its_torque_level_and_spread():
    s = _compare(_torque_buckets(("02", 1.9996, 0.0213), ("03", 2.0531, 0.1163)))
    assert "no-load share 0.0%; torque 1.9996 Nm (sigma 0.0213)." in s.findings
    assert "no-load share 0.0%; torque 2.0531 Nm (sigma 0.1163)." in s.findings


def test_a_step_in_torque_spread_is_named_and_comes_with_a_check():
    s = _compare(_torque_buckets(("02", 1.9996, 0.0213), ("03", 2.0531, 0.1163),
                                 ("04", 2.0203, 0.0861)))
    assert ("**Torque spread.** Sigma is 5.5x wider in 2026-03 (0.1163 Nm, mean "
            "2.0531) than in 2026-02 (0.0213 Nm, mean 1.9996)") in s.findings
    assert "does not see a step like this" in s.findings
    assert "different product or setting ran in 2026-03" in s.next_checks


def test_a_steady_torque_spread_adds_no_finding():
    s = _compare(_torque_buckets(("02", 1.9996, 0.0213), ("03", 2.0010, 0.0230)))
    assert "Torque spread" not in s.findings
    assert "different product" not in s.next_checks


def test_a_bucket_without_torque_figures_still_renders():
    # A result from before torque was added, or a bucket with one cap: no
    # sigma to quote, and no crash.
    bare = _torque_buckets(("02", 2.0, None))
    bare[0].pop("torque_mean"), bare[0].pop("torque_median")
    s = _compare(bare)
    assert "no-load share 0.0%." in s.findings


def test_anomalies_name_the_heads_with_the_most_out_of_band_readings():
    by_head = [{"head_id": 22, "count": 26}, {"head_id": 35, "count": 16},
               {"head_id": 7, "count": 13}, {"head_id": 25, "count": 12}]
    findings = _summarise((PlanStep("anomalies", {}), ToolResult.ok(
        "anomalies", _anomaly_values(threshold_by_head=by_head))))
    assert ("Most readings outside the torque band: head 22 (26), head 35 (16), "
            "head 7 (13); 4 head(s) have at least one.") in findings
    assert "head 25" not in findings


def test_no_per_head_line_when_the_threshold_method_did_not_run():
    findings = _summarise((PlanStep("anomalies", {"method": "deviation"}),
                           ToolResult.ok("anomalies", _anomaly_values(
                               threshold_by_head=[]))))
    assert "outside the torque band:" not in findings


def _day(day, total, failed):
    rate = (total - failed) / total
    return {"day": day, "total": total, "successful": total - failed,
            "failed": failed, "success_rate": rate}


def test_the_weakest_day_shows_its_rejects_and_is_not_sent_for_inspection():
    # Two rejects on a 4,225-cap day is the weakest rate and a trivial one;
    # a day also cannot be "inspected mechanically", a head can.
    days = [_day("2026-03-30", 4225, 2), _day("2026-04-01", 400000, 0),
            _day("2026-04-02", 410000, 0)]
    s = render.summarise(Execution(
        plan=Plan(goal="g", steps=[PlanStep("success_rates", {"by": "day"})]),
        results=[ToolResult.ok("success_rates", days)]))
    assert "Weakest day.** 2026-03-30 at 99.9527% over 4,225 capping operations " \
           "(2 rejected, 100.0% of all 2 rejects; the median day has 0)" in s.findings
    assert "Inspect" not in s.next_checks


def test_a_head_is_still_proposed_for_inspection():
    rows = [{"head_id": 29, "total": 879334, "successful": 878886, "failed": 117,
             "success_rate": 878886 / (878886 + 117)},
            {"head_id": 1, "total": 878947, "successful": 878491, "failed": 69,
             "success_rate": 0.999921}]
    s = render.summarise(Execution(
        plan=Plan(goal="g", steps=[PlanStep("success_rates", {"by": "head"})]),
        results=[ToolResult.ok("success_rates", rows)]))
    assert "Inspect head 29 mechanically" in s.next_checks


def test_several_days_at_the_top_are_counted_not_one_of_them_named():
    days = [_day("2026-03-30", 4225, 2)] + [_day(f"2026-04-{d:02d}", 1000, 0)
                                            for d in range(1, 5)]
    findings = _summarise((PlanStep("success_rates", {"by": "day"}),
                           ToolResult.ok("success_rates", days)))
    assert "4 of 5 days at 100.0000%" in findings
    assert "best:" not in findings


def _reject_rates(*rates):
    return _compare([{**b, "reject_rate": r} for b, r in zip(
        _torque_buckets(*[(f"{m:02d}", 2.0, 0.02) for m in range(2, 2 + len(rates))]),
        rates)])


def test_a_falling_reject_rate_is_stated_as_falling():
    s = _reject_rates(0.000052, 0.000011)
    assert "**Reject rate.** Fell: 0.0052% in 2026-02, 0.0011% in 2026-03." in s.findings


def test_a_rising_reject_rate_is_stated_as_rising():
    s = _reject_rates(0.000011, 0.000052)
    assert "**Reject rate.** Rose: 0.0011% in 2026-02, 0.0052% in 2026-03." in s.findings


def test_a_reject_rate_that_barely_moved_is_called_steady():
    s = _reject_rates(0.000050, 0.000052)
    assert "**Reject rate.** Was steady: 0.0050% in 2026-02, 0.0052% in 2026-03." \
        in s.findings


def test_no_reject_rate_line_when_nothing_was_ever_rejected():
    assert "Reject rate." not in _reject_rates(0.0, 0.0).findings


def test_the_longest_gap_comes_with_a_check_against_the_stop_log():
    gaps = ToolResult.ok("event_gaps", {
        "gaps": [], "gap_count": 227, "total_gap_seconds": 1233360,
        "total_gap_hours": 342.6, "min_seconds": 600,
        "longest_gap": {"start": "2026-03-15 16:51:38",
                        "end": "2026-03-16 21:30:18", "duration_seconds": 103120}})
    s = render.summarise(Execution(
        plan=Plan(goal="g", steps=[PlanStep("event_gaps", {})]), results=[gaps]))
    assert "(2026-03-15 16:51:38 to 2026-03-16 21:30:18)" in s.next_checks
    assert "stop log" in s.next_checks
    assert "Re-run this report next period" not in s.next_checks


def test_idle_findings_name_the_longest_period_and_the_spread_across_heads():
    idle = ToolResult.ok("idle_periods", {
        "periods": [{}, {}, {}], "total_idle_seconds": 37588680, "min_seconds": 300,
        "longest_period": {"head_id": 5, "start": "2026-03-13 19:09:46",
                           "end": "2026-03-14 06:23:53", "cycles": 2182,
                           "duration_seconds": 40447},
        "by_head": [{"head_id": 31, "periods": 79, "total_seconds": 704987},
                    {"head_id": 2, "periods": 79, "total_seconds": 690000}]})
    findings = _summarise((PlanStep("idle_periods", {}), idle))
    assert "Longest: 11.2 h on head 5, 2026-03-13 19:09:46 to 2026-03-14 06:23:53." in findings
    assert "Per head the totals run from 191.7 h (head 2) to 195.8 h (head 31)." in findings


def test_the_daily_rates_check_is_not_proposed_when_the_plan_already_ran_it():
    plan = Plan(goal="g", steps=[PlanStep("success_rates", {"by": "day"}),
                                 PlanStep("anomalies", {})])
    s = render.summarise(Execution(plan=plan, results=[
        ToolResult.ok("success_rates", [_day("2026-03-30", 4225, 2),
                                        _day("2026-04-01", 400000, 0)]),
        ToolResult.ok("anomalies", _anomaly_values())]))
    assert "success_rates by day" not in s.next_checks


def test_overview_states_how_many_closures_succeeded_and_failed():
    # Asked "how many closures ended well?", a model once answered with the
    # total of capping operations, because the overview never printed these.
    findings = _summarise((PlanStep("overview", {}), _OVERVIEW))
    assert "**Outcomes.** 99 successful and 1 rejected closures." in findings


def test_closures_without_a_verdict_are_counted_beside_the_outcomes():
    ov = ToolResult.ok("overview", {**_OVERVIEW.values, "capping_operations": 110})
    findings = _summarise((PlanStep("overview", {}), ov))
    assert "99 successful and 1 rejected closures, and 10 carry no pass/fail verdict." in findings


def test_the_outcomes_line_is_not_repeated_when_success_rates_overall_ran():
    overall = ToolResult.ok("success_rates", {"total": 100, "successful": 99, "failed": 1,
                                              "success_rate": 0.99, "lowest_head": 1})
    findings = _summarise((PlanStep("overview", {}), _OVERVIEW),
                          (PlanStep("success_rates", {"by": "overall"}), overall))
    assert "Outcomes." not in findings and "**Success rate.**" in findings


def _torque(**over):
    v = {"n": 14817976, "mean": 1.9996, "min": 1.282, "max": 2.74,
         "stddev": 0.0211, "median": 1.998}
    v.update(over)
    return ToolResult.ok("torque_stats", v)


def test_the_torque_statistics_of_successful_closures_are_printed():
    # The brief's own example answer: mean, min, max and standard deviation.
    findings = _summarise((PlanStep("torque_stats", {"outcome": "successful"}), _torque()))
    assert ("**Torque (successful closures).** 14,817,976 closures: mean 1.9996 Nm, "
            "min 1.282, max 2.740, median 1.998, sigma 0.0211.") in findings


def test_the_torque_outcome_defaults_to_successful_and_names_failed_closures():
    assert "Torque (successful closures)" in _summarise((PlanStep("torque_stats", {}), _torque()))
    assert "Torque (failed closures)" in _summarise(
        (PlanStep("torque_stats", {"outcome": "failed"}), _torque(n=748)))


def test_a_single_closure_has_no_sigma_to_print():
    findings = _summarise((PlanStep("torque_stats", {}), _torque(n=1, stddev=None)))
    assert "sigma" not in findings and "mean 1.9996 Nm" in findings


def _by_head(*sigmas):
    return ToolResult.ok("torque_stats", [
        {"head_id": i + 1, "n": 1000, "mean": 2.0, "min": 1.9, "max": 2.1,
         "stddev": s, "median": 1.995} for i, s in enumerate(sigmas)])


def test_the_most_variable_head_comes_with_the_range_of_the_others():
    # 0.0248 against 0.0209-0.0217 is a head; 0.0213 against 0.0209-0.0212 is not.
    findings = _summarise((PlanStep("torque_stats", {"by": "head", "outcome": "all"}),
                           _by_head(0.0248, 0.0217, 0.0212, 0.0209)))
    assert ("Head 1 is the most variable over all closures (sigma = 0.0248 Nm about a "
            "median of 1.995 Nm, 17.0% above the median sigma of the other heads); "
            "the other 3 heads run from 0.0209 to 0.0217 Nm.") in findings


def test_the_weakest_head_comes_with_its_share_of_all_rejects():
    rows = [{"head_id": h, "total": 1000, "successful": 1000 - f, "failed": f,
             "success_rate": (1000 - f) / 1000} for h, f in ((1, 20), (2, 90), (3, 22), (4, 4))]
    findings = _summarise((PlanStep("success_rates", {"by": "head"}),
                           ToolResult.ok("success_rates", rows)))
    assert "(90 rejected, 66.2% of all 136 rejects; the median head has 21)" in findings


def _rates_result(by="head"):
    key = "head_id" if by == "head" else "day"
    return ToolResult.ok("success_rates", [
        {key: 1 if by == "head" else "2026-02-01", "total": 411810, "successful": 411588,
         "failed": 45, "success_rate": 0.99989},
        {key: 2 if by == "head" else "2026-02-02", "total": 411903, "successful": 411743,
         "failed": 4, "success_rate": 0.99999}])


def _plan_of(*steps_and_results):
    return Execution(plan=Plan(goal="g", steps=[s for s, _ in steps_and_results]),
                     results=[r for _, r in steps_and_results])


def test_every_head_gets_a_row_in_the_success_rate_table():
    text = render.tables(_plan_of((PlanStep("success_rates", {"by": "head"}), _rates_result())))
    assert "### Success rate per head (table)" in text
    assert "| Head | Closures | Successful | Rejected | Success rate |" in text
    assert "| 1 | 411,810 | 411,588 | 45 | 99.9890% |" in text
    assert "| 2 | 411,903 | 411,743 | 4 | 99.9990% |" in text


def test_a_daily_breakdown_is_a_table_with_one_row_per_day():
    text = render.tables(_plan_of((PlanStep("success_rates", {"by": "day"}), _rates_result("day"))))
    assert "### Success rate per day (table)" in text and "| Day |" in text
    assert "| 2026-02-01 | 411,810 | 411,588 | 45 | 99.9890% |" in text


def test_torque_per_head_is_a_table_naming_the_outcome():
    text = render.tables(_plan_of((PlanStep("torque_stats", {"by": "head", "outcome": "all"}),
                                   _by_head(0.0248, 0.0209))))
    assert "### Torque per head (all closures) (table)" in text
    assert "| 1 | 1,000 | 2.0000 | 1.900 | 2.100 | 0.0248 | 1.995 |" in text


def test_results_that_are_not_a_row_per_head_or_day_make_no_table():
    overall = ToolResult.ok("success_rates", {"total": 1, "successful": 1, "failed": 0,
                                              "success_rate": 1.0, "lowest_head": 1})
    assert render.tables(_plan_of((PlanStep("success_rates", {"by": "overall"}), overall),
                                  (PlanStep("torque_stats", {}), _torque()))) == ""


def test_a_long_table_is_cut_where_asked_and_says_so():
    rows = [{"head_id": h, "total": 10, "successful": 10, "failed": 0, "success_rate": 1.0}
            for h in range(1, 11)]
    text = render.tables(_plan_of((PlanStep("success_rates", {"by": "head"}),
                                   ToolResult.ok("success_rates", rows))), max_rows=3)
    assert "| 3 |" in text and "| 4 |" not in text and "7 more rows not shown" in text


def test_the_report_prints_the_tables_above_the_figures(tiny_cfg, tmp_path):
    _, text = _kpi(tiny_cfg, tmp_path)
    assert "### Success rate per head (table)" in text
    assert text.index("(table)") < text.index("![success rate per head]")


def test_the_daily_tables_can_be_cut_apart_from_the_per_head_ones():
    both = _plan_of((PlanStep("success_rates", {"by": "head"}), _rates_result()),
                    (PlanStep("success_rates", {"by": "day"}), _rates_result("day")))
    text = render.tables(both, max_rows=100, max_day_rows=1)
    assert "| 2 | 411,903 |" in text                      # the head table is whole
    assert "| 2026-02-01 |" in text and "| 2026-02-02 |" not in text
    assert "1 more rows not shown" in text


def test_a_head_barely_above_the_others_says_how_barely():
    # Head 9 at 0.0213 against 0.0208-0.0212 was named "different" by a model;
    # the percentage is what lets a reader (or a model) see it is not.
    findings = _summarise((PlanStep("torque_stats", {"by": "head"}),
                           _by_head(0.0213, 0.0212, 0.0210, 0.0208)))
    assert "1.4% above the median sigma of the other heads" in findings


def _filter(**over):
    v = {"filters": {"above": 2.5, "below": None, "outcome": "all", "head": None},
         "count": 3, "capping_operations": 14824304,
         "by_head": [{"head_id": 22, "count": 2}, {"head_id": 7, "count": 1}],
         "events": [{"head_id": 22, "ts": "2026-02-03 04:05:06", "app_torque": 2.74,
                     "status": 0.0, "outcome": "ok"}], "listed": 1}
    v.update(over)
    return ToolResult.ok("closure_filter", v)


def test_a_threshold_count_states_the_value_the_operator_gave():
    findings = _summarise((PlanStep("closure_filter", {}), _filter()))
    assert ("**Filtered closures.** 3 capping operations with torque above 2.5 Nm "
            "(0.00002% of 14,824,304 capping operations).") in findings
    assert "Most on head 22 (2), head 7 (1); 2 head(s) have at least one." in findings


def test_a_filter_names_outcome_head_and_both_limits():
    f = {"above": 1.5, "below": 2.5, "outcome": "failed", "head": 3}
    findings = _summarise((PlanStep("closure_filter", {}), _filter(
        filters=f, count=10, by_head=[{"head_id": 3, "count": 10}], listed=10)))
    assert "with torque above 1.5 Nm and torque below 2.5 Nm and outcome failed and head 3" in findings
    assert "Most on" not in findings                    # one head: nothing to rank
    assert "All are listed" in findings


def test_a_cut_event_list_says_how_many_are_listed():
    findings = _summarise((PlanStep("closure_filter", {}), _filter(count=70, listed=20)))
    assert "The first 20 are listed in the table below." in findings


def test_a_filter_that_matches_nothing_says_zero():
    findings = _summarise((PlanStep("closure_filter", {}), _filter(
        count=0, by_head=[], events=[], listed=0)))
    assert "0 capping operations with torque above 2.5 Nm (0%" in findings


def test_the_matching_events_are_a_table():
    text = render.tables(_plan_of((PlanStep("closure_filter", {}), _filter())))
    assert "### Matching closures (table)" in text
    assert "| 22 | 2026-02-03 04:05:06 | 2.740 | 0 | ok |" in text


def test_the_documented_method_is_printed_topic_by_topic():
    method = ToolResult.ok("methodology", {"topics": [
        {"topic": "duplicates", "text": "A closure is written once."},
        {"topic": "classification", "text": "Successful when the status is 0."}]})
    findings = _summarise((PlanStep("methodology", {}), method))
    assert "**Method (duplicates).** A closure is written once." in findings
    assert "**Method (classification).** Successful when the status is 0." in findings


def test_a_step_in_torque_level_is_named_even_when_the_spread_is_not_the_story():
    # The weeks of March 2026: median 1.999 Nm, then 1.748, then 2.198.
    buckets = _torque_buckets(("03", 1.999, 0.094), ("04", 1.748, 0.137), ("05", 2.198, 0.004))
    s = _compare(buckets)
    assert "**Torque level.** The mean moves from 1.7480 Nm in 2026-04 to 2.1980 Nm in 2026-05" in s.findings
    assert "0.4500 Nm apart, 112 times the sigma of the steadiest bucket (0.0040 Nm)" in s.findings
    assert "between 2026-04 and 2026-05" in s.next_checks


def test_a_small_move_in_level_is_not_a_step():
    # February to April 2026: 1.9996, 2.0531, 2.0203, 2.5 times the steadiest sigma.
    s = _compare(_torque_buckets(("02", 1.9996, 0.0213), ("03", 2.0531, 0.1163),
                                 ("04", 2.0203, 0.0861)))
    assert "Torque level" not in s.findings


def test_the_planned_figures_are_the_ones_that_get_drawn(tiny_cfg, tmp_path):
    # planned_figures() names figures without drawing them, for a model that is
    # asked to "plot" something; it must not drift from what _figures() writes.
    from analytics.agent.executor import execute
    plan = Plan(goal="g", steps=[
        PlanStep("success_rates", {"by": "head"}), PlanStep("success_rates", {"by": "day"}),
        PlanStep("torque_stats", {"outcome": "successful"}),
        PlanStep("success_rates", {"by": "head"})])      # a repeat gets a step suffix
    ex = execute(tiny_cfg, plan)
    drawn = render._figures(ex, tmp_path)
    assert render.planned_figures(ex) == drawn
    assert [name for _, name in drawn] == [
        "success_rate_per_head.png", "failed_closures_per_day.png",
        "torque_histogram.png", "success_rate_per_head_step4.png"]


def test_a_result_with_nothing_in_it_plans_no_figure():
    empty = ToolResult.insufficient("success_rates", "no capping operations")
    ex = Execution(plan=Plan(goal="g", steps=[PlanStep("success_rates", {"by": "head"})]),
                   results=[empty])
    assert render.planned_figures(ex) == []


def test_a_head_barely_above_the_others_is_said_not_to_stand_out():
    findings = _summarise((PlanStep("torque_stats", {"by": "head"}),
                           _by_head(0.0213, 0.0212, 0.0210, 0.0208)))
    assert findings.rstrip().endswith("No head stands out.")


def test_a_head_well_above_the_others_is_said_to_stand_out():
    findings = _summarise((PlanStep("torque_stats", {"by": "head"}),
                           _by_head(0.0248, 0.0217, 0.0212, 0.0209)))
    assert findings.rstrip().endswith("It stands out from the other heads.")


def test_the_heads_with_most_rejects_are_ranked_by_count_not_by_rate():
    # Head 22 has the lowest rate of the three it is listed with, but head 35 has
    # more rejects: a model reading the table ranked 22 second.
    rows = [{"head_id": h, "total": t, "successful": t - f, "failed": f,
             "success_rate": (t - f) / t}
            for h, t, f in ((29, 411776, 90), (22, 100000, 30), (35, 411811, 54), (30, 411752, 50))]
    findings = _summarise((PlanStep("success_rates", {"by": "head"}),
                           ToolResult.ok("success_rates", rows)))
    assert "Most rejects: head 29 (90), head 35 (54), head 30 (50)." in findings


def test_the_days_with_most_rejects_are_ranked_too():
    days = [_day("2026-02-25", 800000, 100), _day("2026-02-17", 700000, 85),
            _day("2026-02-05", 94248, 14), _day("2026-02-02", 500000, 0)]
    findings = _summarise((PlanStep("success_rates", {"by": "day"}),
                           ToolResult.ok("success_rates", days)))
    assert "Most rejects: day 2026-02-25 (100), day 2026-02-17 (85), day 2026-02-05 (14)." in findings


def test_one_head_with_rejects_has_no_ranking_to_print():
    rows = [{"head_id": 1, "total": 10, "successful": 9, "failed": 1, "success_rate": 0.9},
            {"head_id": 2, "total": 10, "successful": 10, "failed": 0, "success_rate": 1.0}]
    findings = _summarise((PlanStep("success_rates", {"by": "head"}),
                           ToolResult.ok("success_rates", rows)))
    assert "Most rejects" not in findings


def _volume_buckets(*specs):
    """(month, caps, mean torque, sigma) -> compare_periods buckets."""
    buckets = _torque_buckets(*[(m, mean, sd) for m, _, mean, sd in specs])
    for b, (_, caps, _, _) in zip(buckets, specs):
        b["caps"] = caps
    return buckets


def test_a_sliver_of_a_bucket_is_not_compared_with_a_full_one():
    # The store starts on 31 January at 16:00. Its eight hours (0.6% of the
    # capping operations) as "2026-01" gave a sigma 37 times narrower than March's.
    s = _compare(_volume_buckets(("01", 180_000, 1.9976, 0.0031),
                                 ("02", 14_800_000, 1.9996, 0.0213),
                                 ("03", 3_900_000, 2.0531, 0.1163)))
    assert "Torque spread.** Sigma is 5.5x wider in 2026-03" in s.findings
    assert "2026-01 (0.0031" not in s.findings


def test_a_bucket_is_kept_once_it_carries_a_hundredth_of_the_volume():
    s = _compare(_volume_buckets(("02", 2_000_000, 1.9996, 0.0213),
                                 ("03", 21_000, 2.0531, 0.1163)))      # 1.04% of the total
    assert "Torque spread" in s.findings


def test_a_sliver_does_not_set_the_direction_of_the_reject_rate():
    sliver_first = _volume_buckets(("01", 180_000, 2.0, 0.02), ("02", 14_800_000, 2.0, 0.02),
                                   ("03", 3_900_000, 2.0, 0.02))
    for b, rate in zip(sliver_first, (0.0, 0.00005, 0.000052)):
        b["reject_rate"] = rate
    s = _compare(sliver_first)
    # first -> last of the buckets that count is February -> March, a steady rate,
    # not a "rose" from a January that held eight hours.
    assert "**Reject rate.** Was steady: 0.0050% in 2026-02, 0.0052% in 2026-03." in s.findings
