"""Nothing below the CLI raises. That is the whole contract of this module.

Plan 6 deferred one gap: a malformed period raises ValueError out of store.py
rather than returning a ToolResult.error. That was tolerable while a human typed
the period; it is not tolerable now that a language model produces it. The
executor closes it uniformly, for all eight tools at once.
"""
from analytics.agent.executor import execute
from analytics.agent.plan import Plan, PlanStep


def _plan(*steps):
    return Plan(goal="test", steps=list(steps))


def test_a_good_plan_produces_one_ok_result_per_step(tiny_cfg):
    ex = execute(tiny_cfg, _plan(
        PlanStep("overview", {"period": "2026-02"}, "scope"),
        PlanStep("success_rates", {"period": "2026-02", "by": "head"}, "per head"),
    ))
    assert [r.status for r in ex.results] == ["ok", "ok"]
    assert [r.tool for r in ex.results] == ["overview", "success_rates"]


def test_a_malformed_period_becomes_an_error_value_not_an_exception(tiny_cfg):
    ex = execute(tiny_cfg, _plan(
        PlanStep("overview", {"period": "February"}, "scope")))
    assert ex.results[0].status == "error"
    assert "February" in ex.results[0].message


def test_a_later_step_still_runs_after_an_earlier_one_errors(tiny_cfg):
    ex = execute(tiny_cfg, _plan(
        PlanStep("overview", {"period": "nonsense"}, "will fail"),
        PlanStep("overview", {"period": "2026-02"}, "will work"),
    ))
    assert [r.status for r in ex.results] == ["error", "ok"]


def test_an_invalid_step_is_rejected_without_calling_the_tool(tiny_cfg):
    ex = execute(tiny_cfg, _plan(
        PlanStep("success_rates", {"by": "sideways"}, "bad enum")))
    assert ex.results[0].status == "error"
    assert "sideways" in ex.results[0].message


def test_an_unknown_tool_is_rejected(tiny_cfg):
    ex = execute(tiny_cfg, _plan(PlanStep("summon_daemon", {}, "no")))
    assert ex.results[0].status == "error"
    assert "summon_daemon" in ex.results[0].message


def test_null_args_are_dropped_so_tool_defaults_apply(tiny_cfg):
    # The plan schema is flat and requires every key, so the model always emits
    # nulls for arguments it does not care about. Those must not shadow defaults.
    ex = execute(tiny_cfg, _plan(
        PlanStep("success_rates",
                 {"period": "2026-02", "by": None, "outcome": None}, "defaults")))
    assert ex.results[0].status == "ok"
    assert "head_id" in ex.results[0].values[0]      # by="head" default applied


def test_insufficient_data_is_preserved_not_converted(tiny_cfg):
    ex = execute(tiny_cfg, _plan(
        PlanStep("overview", {"period": "2026-07"}, "empty month")))
    assert ex.results[0].status == "insufficient_data"


def test_the_trace_records_every_call_with_its_arguments(tiny_cfg):
    ex = execute(tiny_cfg, _plan(
        PlanStep("overview", {"period": "2026-02"}, "scope"),
        PlanStep("summon_daemon", {}, "no"),
    ))
    assert len(ex.trace) == 2
    assert ex.trace[0] == {
        "step": 1, "tool": "overview", "args": {"period": "2026-02"},
        "rationale": "scope", "status": "ok", "rows_scanned": ex.results[0].provenance.rows_scanned,
        "message": "",
    }
    assert ex.trace[1]["status"] == "error"
    assert ex.trace[1]["tool"] == "summon_daemon"


def test_the_trace_is_json_serialisable(tiny_cfg):
    import json
    ex = execute(tiny_cfg, _plan(PlanStep("overview", {"period": "2026-02"}, "scope")))
    json.dumps(ex.trace)   # must not raise
