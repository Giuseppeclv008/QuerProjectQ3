"""Switching providers and planning tiers, with no model of any kind running.

Every test injects a fake client or a fake transport. The point of these is that
the choice of provider changes what goes on the wire and nothing else: the same
Plan type comes back, the executor cannot tell the difference, and the numbers in
the report are produced by SQL either way.
"""
import json

import pytest

from analytics.agent import llm, planner
from analytics.agent.registry import TOOLS, plan_json_schema
from analytics.agent.router import REPORT_TYPES, canned_plan
from analytics.config import Config, ConfigError


# --------------------------------------------------------------- the config
def test_an_unknown_provider_is_rejected_at_config_time():
    with pytest.raises(ConfigError, match="provider must be one of"):
        Config(store_path="x", provider="huggingface")


def test_an_unknown_planning_tier_is_rejected_at_config_time():
    with pytest.raises(ConfigError, match="planning must be one of"):
        Config(store_path="x", planning="wing-it")


def test_ollama_rejects_a_context_too_small_for_the_planner_prompt():
    """Ollama's default num_ctx is 2048 and it truncates silently rather than
    erroring, so a too-small window would look like a stupid model."""
    with pytest.raises(ConfigError, match="num_ctx must be >= 4096"):
        Config(store_path="x", provider="ollama", num_ctx=2048)


def test_the_default_provider_is_unchanged():
    cfg = Config(store_path="x")
    assert (cfg.provider, cfg.planning) == ("anthropic", "plan")


# --------------------------------------------------------------- the schema
def test_the_per_tool_schema_gives_each_tool_only_its_own_arguments():
    """The flat schema shows all nine arguments on every step, which is how a
    real 7B came to hand `trend` an `outcome` it does not take."""
    branches = plan_json_schema("per_tool")["properties"]["steps"]["items"]["anyOf"]
    assert len(branches) == len(TOOLS)
    for branch in branches:
        [name] = branch["properties"]["tool"]["enum"]
        offered = set(branch["properties"]["args"]["properties"])
        assert offered == set(TOOLS[name].params), name
    trend = next(b for b in branches if b["properties"]["tool"]["enum"] == ["trend"])
    assert "outcome" not in trend["properties"]["args"]["properties"]


def test_the_flat_schema_still_offers_the_union_to_every_tool():
    flat = plan_json_schema("flat")["properties"]["steps"]["items"]
    args = flat["properties"]["args"]
    assert set(args["required"]) == set(args["properties"])
    assert "outcome" in args["properties"] and "window" in args["properties"]


def test_an_unknown_schema_style_is_a_programming_error():
    with pytest.raises(ValueError, match="style must be"):
        plan_json_schema("freeform")


# ------------------------------------------------------------ the transport
class _FakeOllama:
    """Stands in for the HTTP round trip, capturing the request body."""

    def __init__(self, content):
        self._content, self.bodies = content, []

    def chat(self, body):
        self.bodies.append(body)
        return {"message": {"content": self._content}}


def _ollama_cfg(**kw):
    return Config(store_path="x", provider="ollama", model="qwen2.5:7b", **kw)


def test_the_ollama_request_omits_every_anthropic_only_field():
    """thinking and output_config are Anthropic's; sending them to Ollama is at
    best ignored and at worst a 400."""
    client = _FakeOllama('{"ok": true}')
    payload, reason = llm.json_call(_ollama_cfg(), client, "sys", "prompt", {"x": 1})
    assert (payload, reason) == ({"ok": True}, None)
    body = client.bodies[0]
    for banned in ("thinking", "output_config", "temperature_top_p", "max_tokens"):
        assert banned not in body
    assert body["format"] == {"x": 1}
    assert body["options"]["num_ctx"] == 8192
    assert body["messages"][0] == {"role": "system", "content": "sys"}


def test_an_ollama_reply_that_is_not_json_degrades_like_any_other():
    client = _FakeOllama("I think the answer is head 12")
    payload, reason = llm.json_call(_ollama_cfg(), client, "s", "p", {})
    assert payload is None and "not valid JSON" in reason


def test_an_empty_ollama_reply_is_reported_distinctly():
    payload, reason = llm.json_call(_ollama_cfg(), _FakeOllama(""), "s", "p", {})
    assert payload is None and reason == "the reply carried no content"


def test_an_unreachable_ollama_daemon_yields_no_client_not_an_exception():
    cfg = _ollama_cfg(ollama_host="http://localhost:1")   # nothing listens here
    assert llm.client(cfg) is None


# ---------------------------------------------------------- planning tiers
class _TierClient:
    """Returns a canned JSON reply and records the schema it was constrained to."""

    def __init__(self, reply):
        self._reply, self.schemas, self.prompts = reply, [], []

    def chat(self, body):
        self.schemas.append(body["format"])
        self.prompts.append(body["messages"][-1]["content"])
        return {"message": {"content": json.dumps(self._reply)}}


def test_classify_turns_one_word_into_the_matching_canned_plan(tiny_cfg):
    cfg = _ollama_cfg(planning="classify")
    client = _TierClient({"report": "drift"})
    p = planner.plan(cfg, "is head 12 getting worse?", "2026-02", client=client)

    assert p.source == "llm"
    assert p.steps == canned_plan("drift", "2026-02").steps
    assert "chose the drift report" in p.note
    # The whole point of the tier: a prompt the size of the question.
    assert len(client.prompts[0]) < 200
    assert client.schemas[0]["properties"]["report"]["enum"] == list(REPORT_TYPES)


def test_classify_falls_back_when_the_model_invents_a_report_type(tiny_cfg):
    cfg = _ollama_cfg(planning="classify")
    p = planner.plan(cfg, "any anomalies?", "2026-02",
                     client=_TierClient({"report": "maintenance"}))
    assert p.source == "router"
    assert "maintenance" in p.note


def test_select_builds_steps_from_tool_names_and_leaves_args_at_defaults(tiny_cfg):
    cfg = _ollama_cfg(planning="select")
    client = _TierClient({"goal": "g", "tools": ["trend", "head_correlation"]})
    p = planner.plan(cfg, "is anything drifting?", "2026-02", client=client)

    assert p.source == "llm"
    assert [s.tool for s in p.steps] == ["trend", "head_correlation"]
    # Only the period is set; everything else is the tool's own default.
    assert all(s.args == {"period": "2026-02"} for s in p.steps)
    assert "arguments are the defaults" in p.note


def test_select_falls_back_on_an_invented_tool(tiny_cfg):
    cfg = _ollama_cfg(planning="select")
    p = planner.plan(cfg, "q", None,
                     client=_TierClient({"goal": "g", "tools": ["predict_failures"]}))
    assert p.source == "router"
    assert "predict_failures" in p.note


def test_select_falls_back_on_an_empty_selection(tiny_cfg):
    cfg = _ollama_cfg(planning="select")
    p = planner.plan(cfg, "q", None, client=_TierClient({"goal": "g", "tools": []}))
    assert p.source == "router"
    assert "selected no tools" in p.note


def test_plan_tier_on_ollama_is_constrained_to_the_per_tool_schema(tiny_cfg):
    cfg = _ollama_cfg(planning="plan")
    client = _TierClient({"goal": "g", "steps": [
        {"tool": "anomalies", "args": {"period": "2026-02"}, "rationale": "r"}]})
    p = planner.plan(cfg, "any anomalies?", "2026-02", client=client)

    assert p.source == "llm"
    assert [s.tool for s in p.steps] == ["anomalies"]
    assert "anyOf" in client.schemas[0]["properties"]["steps"]["items"]


def test_every_tier_produces_steps_the_executor_can_run(tiny_cfg):
    """Whatever the tier, the result is an ordinary Plan of registry-valid steps
    -- which is what keeps the numbers identical across all of them."""
    from dataclasses import replace as _replace

    from analytics.agent.plan import effective_args
    from analytics.agent.registry import validate_step

    replies = {
        "classify": {"report": "kpi"},
        "select": {"goal": "g", "tools": ["overview", "success_rates"]},
        "plan": {"goal": "g", "steps": [
            {"tool": "overview", "args": {"period": "2026-02"}, "rationale": "r"}]},
    }
    for tier, reply in replies.items():
        cfg = _ollama_cfg(planning=tier)
        p = planner.plan(cfg, "how many caps?", "2026-02", client=_TierClient(reply))
        assert p.source == "llm", tier
        assert p.steps, tier
        for step in p.steps:
            assert validate_step(
                _replace(step, args=effective_args(step))) is None, (tier, step)
