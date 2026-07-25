"""The report structure is mandated by the brief (slide 7); it is pinned here.

The golden test is the regression net the spec asks for: a fixed store plus a
fixed plan must render byte-identical Markdown, so a change in any tool's SQL
shows up as a diff in a committed deliverable rather than as a silent shift in a
number nobody re-read.
"""
import json
from pathlib import Path

from analytics.agent.executor import execute
from analytics.agent.router import canned_plan
from analytics.report import render

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
