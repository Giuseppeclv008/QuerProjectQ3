"""Orchestration is tested with a mocked model: no tokens, no network.

The three cases that matter are a good plan, a hallucinated plan, and no API key
at all. In all three the user must end up with real numbers -- the difference is
only whether the limits section says a model was involved.
"""
import json

import pytest

from analytics.agent import planner, router


class _FakeBlock:
    def __init__(self, text):
        self.type, self.text = "text", text


class _FakeResponse:
    def __init__(self, payload):
        self.content = [_FakeBlock(json.dumps(payload))]
        self.stop_reason = "end_turn"


class _FakeMessages:
    def __init__(self, payload=None, raises=None):
        self._payload, self._raises = payload, raises
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises:
            raise self._raises
        return _FakeResponse(self._payload)


class _FakeClient:
    def __init__(self, payload=None, raises=None):
        self.messages = _FakeMessages(payload, raises)


_GOOD = {
    "goal": "Find the head with the lowest success rate.",
    "steps": [
        {"tool": "success_rates",
         "args": {"period": "2026-02", "by": "head", "outcome": None,
                  "bucket": None, "method": None, "signal": None,
                  "window": None, "min_seconds": None, "heads": None},
         "rationale": "Per-head rates answer the question directly."},
    ],
}


def test_a_valid_model_plan_is_used(tiny_cfg):
    client = _FakeClient(_GOOD)
    plan = planner.plan(tiny_cfg, "which head is worst?", "2026-02", client=client)
    assert plan.source == "llm"
    assert [s.tool for s in plan.steps] == ["success_rates"]
    assert plan.steps[0].args["by"] == "head"


def test_nulls_survive_into_the_plan_for_the_executor_to_drop(tiny_cfg):
    plan = planner.plan(tiny_cfg, "q", "2026-02", client=_FakeClient(_GOOD))
    assert plan.steps[0].args["window"] is None


def test_the_request_carries_the_configured_model_and_effort(tiny_cfg):
    client = _FakeClient(_GOOD)
    planner.plan(tiny_cfg, "q", "2026-02", client=client)
    sent = client.messages.calls[0]
    assert sent["model"] == tiny_cfg.model
    assert sent["output_config"]["effort"] == tiny_cfg.effort
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["max_tokens"] == tiny_cfg.max_tokens
    # This model rejects all four of these with a 400. budget_tokens especially:
    # it is the pre-4.6 way to size thinking, so it is the one most likely to be
    # reintroduced by someone working from memory.
    for banned in ("temperature", "top_p", "top_k", "budget_tokens"):
        assert banned not in sent, f"{banned} is rejected by {tiny_cfg.model}"
    assert "budget_tokens" not in sent["thinking"]


def test_the_request_constrains_the_model_to_the_plan_schema(tiny_cfg):
    from analytics.agent import registry
    client = _FakeClient(_GOOD)
    planner.plan(tiny_cfg, "q", None, client=client)
    fmt = client.messages.calls[0]["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"] == registry.plan_json_schema()


def test_a_hallucinated_tool_falls_back_to_the_router(tiny_cfg):
    bad = {"goal": "g", "steps": [{"tool": "predict_failures",
                                   "args": {k: None for k in _GOOD["steps"][0]["args"]},
                                   "rationale": "invented"}]}
    plan = planner.plan(tiny_cfg, "any anomalies?", "2026-02",
                        client=_FakeClient(bad))
    assert plan.source == "router"
    assert "predict_failures" in plan.note
    assert plan.steps == router.canned_plan("anomalies", "2026-02").steps


def test_a_real_argument_the_tool_does_not_take_falls_back(tiny_cfg):
    """Nulls are dropped before validation because the schema forces the model to
    emit them. A foreign argument with a real value is a genuine mistake and must
    still be caught."""
    bad = {"goal": "g",
           "steps": [{"tool": "success_rates",
                      "args": {**{k: None for k in _GOOD["steps"][0]["args"]},
                               "period": "2026-02", "window": 7},
                      "rationale": "window is not a success_rates argument"}]}
    plan = planner.plan(tiny_cfg, "any anomalies?", "2026-02", client=_FakeClient(bad))
    assert plan.source == "router"
    assert "window" in plan.note


def test_an_empty_plan_falls_back_to_the_router(tiny_cfg):
    plan = planner.plan(tiny_cfg, "is anything drifting?", None,
                        client=_FakeClient({"goal": "g", "steps": []}))
    assert plan.source == "router"
    assert plan.steps == router.canned_plan("drift", None).steps


def test_malformed_json_falls_back_to_the_router(tiny_cfg):
    client = _FakeClient(_GOOD)
    client.messages.create = lambda **kw: type(
        "R", (), {"content": [_FakeBlock("not json at all")], "stop_reason": "end_turn"}
    )()
    plan = planner.plan(tiny_cfg, "q", None, client=client)
    assert plan.source == "router"
    assert "json" in plan.note.lower()


def test_an_api_error_falls_back_to_the_router(tiny_cfg):
    plan = planner.plan(tiny_cfg, "any anomalies?", "2026-02",
                        client=_FakeClient(raises=RuntimeError("connection refused")))
    assert plan.source == "router"
    assert "connection refused" in plan.note


def test_a_refusal_falls_back_to_the_router(tiny_cfg):
    client = _FakeClient(_GOOD)
    client.messages.create = lambda **kw: type(
        "R", (), {"content": [], "stop_reason": "refusal"})()
    plan = planner.plan(tiny_cfg, "q", None, client=client)
    assert plan.source == "router"
    assert "refus" in plan.note.lower()


def test_no_api_key_falls_back_without_a_network_call(tiny_cfg, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(planner, "_client", lambda cfg: None)
    plan = planner.plan(tiny_cfg, "which head is worst?", "2026-02")
    assert plan.source == "router"
    assert "no anthropic client" in plan.note.lower()


def test_the_system_prompts_tool_advice_matches_what_the_router_would_do():
    """planner.SYSTEM restates, in prose, the routing rules router.route()
    implements in code. Nothing links them, so they can drift apart silently and
    the model would be steered somewhere the fallback would not go. This pins the
    signals the prompt names against the router's own keyword table."""
    from analytics.agent import planner as p
    from analytics.agent.router import _KEYWORDS

    advice = p.SYSTEM.lower()
    for phrase, report_type in (("changed over time", "drift"),
                                ("head is unusual", "drift"),
                                ("out of range", "anomalies"),
                                ("how much or how", "kpi")):
        assert phrase in advice, f"SYSTEM no longer advises on {phrase!r}"

    # Each tool the prompt names must exist, and must be the tool the matching
    # canned plan actually runs.
    from analytics.agent.registry import TOOLS
    from analytics.agent.router import canned_plan
    for tool, report_type in (("trend", "drift"), ("head_correlation", "drift"),
                              ("anomalies", "anomalies"), ("overview", "kpi"),
                              ("success_rates", "kpi")):
        assert tool in TOOLS, f"SYSTEM names {tool!r}, which is not a tool"
        planned = {s.tool for s in canned_plan(report_type, None).steps}
        assert tool in planned, (
            f"SYSTEM steers {tool!r} at questions the router sends to the "
            f"{report_type!r} plan, which runs {sorted(planned)}")
    assert set(_KEYWORDS) == {"drift", "anomalies", "kpi"}
