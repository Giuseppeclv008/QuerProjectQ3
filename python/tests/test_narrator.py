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


def test_a_huge_result_list_does_not_become_a_huge_prompt(tiny_cfg):
    """On the real store `anomalies` returns 678,325 deviation hits. Serialising
    them in full builds a multi-megabyte prompt no request can carry, so `ask`
    would fail on every question about real data -- silently, via the template
    fallback."""
    from analytics.agent.executor import Execution
    from analytics.agent.plan import Plan, PlanStep
    from analytics.result import ToolResult

    hits = [{"ts": "2026-02-01 00:00:00", "head_id": h % 36, "app_torque": 2.0}
            for h in range(200_000)]
    ex = Execution(
        plan=Plan(goal="g", steps=[PlanStep("anomalies", {})]),
        results=[ToolResult.ok("anomalies",
                               {"deviation_hits": hits, "threshold_hits": [],
                                "counts": {"deviation_hits": len(hits)}})])
    client = _Client(_GOOD)
    narrator.narrate(tiny_cfg, ex, client=client)
    prompt = client.calls[0]["messages"][0]["content"]

    assert len(prompt) < 100_000, f"prompt is {len(prompt):,} chars"
    # The count survives -- it is what the narrator actually needs.
    assert "200000" in prompt
    assert "truncated" in prompt


def test_a_normal_per_head_breakdown_is_not_truncated(tiny_cfg):
    """The bound must not clip an analytic grouping: 36 heads (48 on the brief's
    example machine) or the days in a period must reach the model whole."""
    from analytics.agent.executor import Execution
    from analytics.agent.plan import Plan, PlanStep
    from analytics.result import ToolResult

    per_head = [{"head_id": h, "success_rate": 0.99} for h in range(1, 49)]
    ex = Execution(plan=Plan(goal="g", steps=[PlanStep("success_rates", {"by": "head"})]),
                   results=[ToolResult.ok("success_rates", per_head)])
    client = _Client(_GOOD)
    narrator.narrate(tiny_cfg, ex, client=client)
    prompt = client.calls[0]["messages"][0]["content"]

    assert "truncated" not in prompt
    assert '"head_id": 48' in prompt


def test_the_reason_the_model_did_not_narrate_reaches_the_report(tiny_cfg, tmp_path):
    """Without this the report shows `plan source: llm, narrative source:
    template` and never says why -- the planner discloses its fallback reason and
    the narrator did not."""
    ex = _execution(tiny_cfg)
    n = narrator.narrate(tiny_cfg, ex, client=_Client(raises=RuntimeError("429 rate limit")))
    assert "429 rate limit" in n.note

    text = render.render(ex, tiny_cfg, tmp_path, n, generated_at="fixed")
    assert "**Narration.**" in text
    assert "429 rate limit" in text


def test_findings_that_state_no_number_fall_back_to_the_template(tiny_cfg):
    """Verbatim from a local 7B on real results: an announcement of findings
    rather than findings. Structured outputs guarantee a string arrives, not
    that it says anything."""
    empty = {"findings": "The analysis reveals several key insights and potential "
                         "issues. Here's a summary of the findings from both the "
                         "correlation matrix and drift analysis tools:",
             "next_checks": "- Review the above."}
    ex = _execution(tiny_cfg)
    n = narrator.narrate(tiny_cfg, ex, client=_Client(empty))

    assert n.source == "template"
    assert "no bullet" in n.note
    assert n.findings == render.summarise(ex).findings


def test_a_blank_findings_section_falls_back_too(tiny_cfg):
    ex = _execution(tiny_cfg)
    n = narrator.narrate(tiny_cfg, ex,
                         client=_Client({"findings": "   ", "next_checks": "- x"}))
    assert n.source == "template"
    assert "empty findings" in n.note


def test_real_findings_with_numbers_are_kept(tiny_cfg):
    """The guard must not eat a genuine narrative."""
    good = {"findings": "- Head 2 rejected 2 of 3 caps (66.67% success).",
            "next_checks": "- Inspect head 2."}
    n = narrator.narrate(tiny_cfg, _execution(tiny_cfg), client=_Client(good))
    assert n.source == "llm"
    assert "Head 2" in n.findings
