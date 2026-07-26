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
    assert trace == ex.trace


def test_report_md_is_written_to_disk(tiny_cfg, tmp_path):
    _, text = _kpi(tiny_cfg, tmp_path)
    assert (tmp_path / "report.md").read_text() == text


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
    assert "not-a-month" in limits or "error" in limits.lower()


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
    assert text == GOLDEN.read_text(), (
        "The KPI report changed. If that is intentional, regenerate the golden "
        "with `../.venv/bin/python -m tests.regen_golden` -- then read the diff."
    )
