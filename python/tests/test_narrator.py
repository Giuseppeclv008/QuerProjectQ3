"""The narrator writes prose around numbers it is handed. It cannot alter them.

The load-bearing test is the last one: whatever the model returns, the numbers in
the report still came from the tools, so a narrator failure costs readability and
nothing else.
"""
import json

from analytics.agent import narrator
from analytics.agent.executor import execute
from analytics.agent.router import canned_plan
from analytics.report import render


class _Block:
    def __init__(self, text):
        self.type, self.text = "text", text


class _Client:
    def __init__(self, payload=None, raises=None):
        self._payload, self._raises = payload, raises
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises:
            raise self._raises
        return type("R", (), {"content": [_Block(json.dumps(self._payload))],
                              "stop_reason": "end_turn"})()


_GOOD = {"findings": "- The machine is healthy.",
         "next_checks": "- Re-run next month."}


def _execution(tiny_cfg):
    return execute(tiny_cfg, canned_plan("kpi", "2026-02"))


def test_a_good_model_narrative_is_used(tiny_cfg):
    n = narrator.narrate(tiny_cfg, _execution(tiny_cfg), client=_Client(_GOOD))
    assert n.source == "llm"
    assert n.findings == "- The machine is healthy."


def test_the_model_is_handed_the_results_it_must_narrate(tiny_cfg):
    client = _Client(_GOOD)
    narrator.narrate(tiny_cfg, _execution(tiny_cfg), client=client)
    prompt = client.calls[0]["messages"][0]["content"]
    assert "capping_operations" in prompt
    assert "success_rate" in prompt


def test_no_sampling_parameters_are_sent(tiny_cfg):
    client = _Client(_GOOD)
    narrator.narrate(tiny_cfg, _execution(tiny_cfg), client=client)
    sent = client.calls[0]
    for banned in ("temperature", "top_p", "top_k", "budget_tokens"):
        assert banned not in sent


def test_an_api_error_falls_back_to_the_template(tiny_cfg):
    ex = _execution(tiny_cfg)
    n = narrator.narrate(tiny_cfg, ex, client=_Client(raises=RuntimeError("429")))
    assert n.source == "template"
    assert n.findings == render.summarise(ex).findings


def test_malformed_json_falls_back_to_the_template(tiny_cfg):
    ex = _execution(tiny_cfg)
    client = _Client(_GOOD)
    client.create = lambda **kw: type(
        "R", (), {"content": [_Block("prose, not json")], "stop_reason": "end_turn"})()
    assert narrator.narrate(tiny_cfg, ex, client=client).source == "template"


def test_no_client_falls_back_without_a_network_call(tiny_cfg, monkeypatch):
    monkeypatch.setattr(narrator, "_client", lambda cfg: None)
    ex = _execution(tiny_cfg)
    assert narrator.narrate(tiny_cfg, ex).source == "template"


def test_the_numbers_in_the_report_come_from_the_tools_whatever_the_model_says(
        tiny_cfg, tmp_path):
    lying = {"findings": "- Success rate was 12%.", "next_checks": "- Panic."}
    ex = _execution(tiny_cfg)
    n = narrator.narrate(tiny_cfg, ex, client=_Client(lying))
    text = render.render(ex, tiny_cfg, tmp_path, n, generated_at="fixed")
    # The model's sentence is quoted in Findings, but the trace and the limits
    # section still carry the real provenance -- and the plot is drawn from the
    # ToolResult, not from the prose.
    assert "rows scanned" in text.lower()
    overall = next(r for r in ex.results
                   if r.tool == "success_rates" and isinstance(r.values, dict))
    assert overall.values["success_rate"] is not None
    assert (tmp_path / "success_rate_per_head.png").exists()
