"""The router is the offline fallback and the reproducible demo path.

Its plans are what `report kpi|drift|anomalies` run, so they are pinned exactly:
a change here changes a committed deliverable.
"""
import pytest

from analytics.agent import router


def test_kpi_plan_is_pinned():
    plan = router.canned_plan("kpi", "2026-02")
    assert [s.tool for s in plan.steps] == [
        "overview", "success_rates", "success_rates", "capping_speed", "idle_periods",
    ]
    assert plan.steps[1].args["by"] == "overall"
    assert plan.steps[2].args["by"] == "head"
    assert all(s.args["period"] == "2026-02" for s in plan.steps)
    assert plan.source == "router"


def test_drift_plan_is_pinned():
    plan = router.canned_plan("drift", "2026-02..2026-04")
    assert [s.tool for s in plan.steps] == [
        "trend", "trend", "torque_stats", "head_correlation",
    ]
    assert plan.steps[0].args["signal"] == "torque"
    assert plan.steps[1].args["signal"] == "success_rate"
    assert all(s.args["period"] == "2026-02..2026-04" for s in plan.steps)


def test_anomaly_plan_is_pinned():
    plan = router.canned_plan("anomalies", None)
    assert [s.tool for s in plan.steps] == ["anomalies", "overview", "success_rates"]
    assert plan.steps[0].args["method"] == "both"
    assert all(s.args["period"] is None for s in plan.steps)


def test_every_step_of_every_canned_plan_validates():
    from analytics.agent import registry
    for report_type in router.REPORT_TYPES:
        for step in router.canned_plan(report_type, "2026-02").steps:
            assert registry.validate_step(step) is None, f"{report_type}: {step}"


def test_every_canned_plan_carries_a_rationale():
    for report_type in router.REPORT_TYPES:
        for step in router.canned_plan(report_type, None).steps:
            assert step.rationale, f"{report_type}.{step.tool} has no rationale"


def test_unknown_report_type_raises():
    with pytest.raises(ValueError, match="quarterly"):
        router.canned_plan("quarterly", None)


@pytest.mark.parametrize("question,expected", [
    ("is head 4 drifting?", "drift"),
    ("did the average torque change over the month?", "drift"),
    ("are there torque values outside the expected range?", "anomalies"),
    ("which closures were rejected?", "anomalies"),
    ("what percentage of capping operations were successful?", "kpi"),
    ("how many closure events were performed by each head?", "kpi"),
])
def test_routing_picks_the_nearest_plan(question, expected):
    plan = router.route(question, "2026-02")
    assert plan.steps == router.canned_plan(expected, "2026-02").steps


def test_routing_defaults_to_kpi_and_says_so():
    plan = router.route("hello there", None)
    assert plan.steps == router.canned_plan("kpi", None).steps
    assert "no keyword" in plan.note.lower()


def test_routed_plan_keeps_the_users_question_as_the_goal():
    plan = router.route("which head is worst?", None)
    assert "which head is worst?" in plan.goal
