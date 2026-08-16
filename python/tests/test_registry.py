"""The registry is the load-bearing interface: WP2 and WP3 cannot drift apart.

If a tool exists in analytics.tools but not here, the agent can never call it.
If it exists here with the wrong signature, the executor blows up at run time
instead of at import time. Both are pinned below.
"""
import inspect

import pytest

from analytics.agent import registry
from analytics.agent.plan import PlanStep


def test_every_wp2_tool_is_registered():
    assert set(registry.TOOLS) == {
        "overview", "success_rates", "torque_stats", "capping_speed",
        "idle_periods", "anomalies", "trend", "head_correlation",
    }


def test_every_registered_param_exists_on_the_callable():
    for name, spec in registry.TOOLS.items():
        sig = inspect.signature(spec.fn)
        for param in spec.params:
            assert param in sig.parameters, f"{name}.{param} is not a real argument"


def test_every_optional_callable_arg_is_registered():
    # cfg is positional and supplied by the executor; everything else the agent
    # may set must be describable, or the agent can never reach it.
    for name, spec in registry.TOOLS.items():
        sig = inspect.signature(spec.fn)
        settable = [p for p in sig.parameters if p != "cfg"]
        assert set(settable) == set(spec.params), f"{name} params drifted"


def test_tool_schemas_are_well_formed():
    schemas = registry.tool_schemas()
    assert len(schemas) == len(registry.TOOLS)
    for s in schemas:
        assert s["name"] in registry.TOOLS
        assert s["description"]
        assert s["input_schema"]["type"] == "object"
        assert s["input_schema"]["additionalProperties"] is False


def test_validate_step_accepts_a_good_step():
    step = PlanStep(tool="success_rates", args={"by": "head"}, rationale="per-head KPI")
    assert registry.validate_step(step) is None


def test_validate_step_rejects_an_unknown_tool():
    step = PlanStep(tool="predict_the_future", args={}, rationale="")
    reason = registry.validate_step(step)
    assert reason is not None and "predict_the_future" in reason


def test_validate_step_rejects_an_unknown_argument():
    step = PlanStep(tool="overview", args={"by": "head"}, rationale="")
    reason = registry.validate_step(step)
    assert reason is not None and "by" in reason


def test_validate_step_rejects_a_bad_enum_value():
    step = PlanStep(tool="success_rates", args={"by": "sideways"}, rationale="")
    reason = registry.validate_step(step)
    assert reason is not None and "sideways" in reason


def test_plan_json_schema_lists_every_tool_in_its_enum():
    schema = registry.plan_json_schema()
    enum = schema["properties"]["steps"]["items"]["properties"]["tool"]["enum"]
    assert set(enum) == set(registry.TOOLS)


def test_plan_schema_unions_enums_that_clash_across_tools():
    # `by` means head|day|overall to success_rates, head to torque_stats, and
    # day|hour to trend/head_correlation. If the flat schema let one tool win,
    # the model could never legally emit the KPI report's success_rates(by="head").
    args = (registry.plan_json_schema()["properties"]["steps"]["items"]
            ["properties"]["args"]["properties"])
    assert set(args["by"]["enum"]) == {"head", "day", "overall", "hour", None}


def test_plan_schema_requires_every_argument_key():
    # Structured outputs need a closed object: every property must be required,
    # which is why the model emits explicit nulls and the executor drops them.
    items = registry.plan_json_schema()["properties"]["steps"]["items"]
    args = items["properties"]["args"]
    assert set(args["required"]) == set(args["properties"])
    assert args["additionalProperties"] is False


def test_strict_validation_still_rejects_a_value_the_union_allows():
    # The permissive schema is not the gate -- validate_step is.
    step = PlanStep(tool="success_rates", args={"by": "hour"}, rationale="")
    assert registry.validate_step(step) is not None


def test_validate_step_rejects_types_and_bounds_not_just_names():
    # "Schema permissive, validation strict" -- these four were the probes
    # that walked through when the gate checked names and enums only.
    from analytics.agent.plan import PlanStep as Step
    from analytics.agent.registry import validate_step
    assert validate_step(Step("trend", {"window": -5}, "")) is not None
    assert validate_step(Step("trend", {"window": 0}, "")) is not None
    assert validate_step(Step("idle_periods", {"min_seconds": "not-an-int"}, "")) is not None
    assert validate_step(Step("head_correlation", {"heads": list(range(100000))}, "")) is not None
    assert validate_step(Step("head_correlation", {"heads": [0]}, "")) is not None
    # and the valid shapes still pass
    assert validate_step(Step("trend", {"window": 7}, "")) is None
    assert validate_step(Step("head_correlation", {"heads": [1, 5]}, "")) is None
    assert validate_step(Step("trend", {"window": None}, "")) is None
