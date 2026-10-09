"""The narrator writes prose around numbers it is handed. It cannot alter them.

The load-bearing test is the last one: whatever the model returns, the numbers in
the report still came from the tools, so a narrator failure costs readability and
nothing else.
"""
import json
from dataclasses import replace

import pytest

from analytics.agent import narrator
from analytics.agent.executor import execute
from analytics.agent.router import canned_plan
from analytics.report import render


@pytest.fixture
def tiny_cfg(tiny_cfg):
    """The fakes here speak the Anthropic messages shape, and Ollama is the
    default provider, so pin the provider these tests were written against."""
    return replace(tiny_cfg, provider="anthropic", model="claude-opus-5")


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
    """On the real store `anomalies` returns 162,019 deviation hits on February
    alone. Serialising them in full builds a multi-megabyte prompt no request can
    carry, so `ask` would fail on every question about real data -- silently, via
    the template fallback."""
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


def test_bounded_truncates_long_strings_not_only_long_lists():
    # A 500 KB error message once reached the prompt whole; _bounded's cap
    # must cover strings wherever they sit.
    from analytics.agent.narrator import _MAX_STR_CHARS, _bounded
    long = "x" * (_MAX_STR_CHARS + 500)
    out = _bounded(long, 10)
    assert len(out) < _MAX_STR_CHARS + 100
    assert "truncated" in out
    nested = _bounded({"message": long, "items": [long]}, 10)
    assert "truncated" in nested["message"]
    assert "truncated" in nested["items"][0]


@pytest.mark.parametrize("findings", [
    "1. March produced a quarter of February's volume.",
    "1) March produced a quarter of February's volume.",
    "• March produced a quarter of February's volume.",
])
def test_a_numbered_or_dotted_list_is_a_bulleted_list(tiny_cfg, findings):
    n = narrator.narrate(tiny_cfg, _execution(tiny_cfg),
                         client=_Client({"findings": findings,
                                         "next_checks": "- Check March."}))
    assert n.source == "llm"
    assert n.findings == findings


def test_rejected_findings_are_logged_so_the_cause_is_visible(tiny_cfg, caplog):
    prose = "Here are my findings about the machine:"
    with caplog.at_level("WARNING", logger="analytics.agent.narrator"):
        n = narrator.narrate(tiny_cfg, _execution(tiny_cfg),
                             client=_Client({"findings": prose,
                                             "next_checks": "- x"}))
    assert n.source == "template"
    assert prose in caplog.text


def test_the_logged_excerpt_is_bounded(tiny_cfg, caplog):
    prose = "x" * 2000
    with caplog.at_level("WARNING", logger="analytics.agent.narrator"):
        narrator.narrate(tiny_cfg, _execution(tiny_cfg),
                         client=_Client({"findings": prose, "next_checks": "- x"}))
    assert "x" * 500 in caplog.text
    assert "x" * 501 not in caplog.text


def test_the_operators_question_reaches_the_narrator_verbatim(tiny_cfg):
    """The planner rewrites the question into a goal; the narrator must see
    both, so its first finding answers what was asked, not the rewording."""
    ex = _execution(tiny_cfg)
    ex = replace(ex, plan=replace(ex.plan, question="Did the machine get worse?"))
    client = _Client(_GOOD)
    narrator.narrate(tiny_cfg, ex, client=client)
    prompt = client.calls[0]["messages"][0]["content"]
    assert "<question>\nDid the machine get worse?\n</question>" in prompt
    assert f"<goal>\n{ex.plan.goal}\n</goal>" in prompt


def test_the_system_prompt_asks_for_a_direct_answer_first():
    rules = narrator.SYSTEM.lower()
    assert "first bullet" in rules
    assert "cannot" in rules and "answer" in rules
    assert "threshold" in rules


def test_the_schema_asks_for_lists_not_one_markdown_string():
    props = narrator._SCHEMA["properties"]
    assert props["findings"]["type"] == "array"
    assert props["next_checks"]["type"] == "array"
    assert props["findings"]["items"] == {"type": "string"}


def test_a_list_becomes_one_bullet_per_item(tiny_cfg):
    reply = {"findings": ["March produced a quarter of February's volume.",
                          "The reject rate fell."],
             "next_checks": ["Run event_gaps over March."]}
    n = narrator.narrate(tiny_cfg, _execution(tiny_cfg), client=_Client(reply))
    assert n.source == "llm"
    assert n.findings == ("- March produced a quarter of February's volume.\n"
                          "- The reject rate fell.")
    assert n.next_checks == "- Run event_gaps over March."


def test_a_marker_left_in_an_item_is_not_printed_twice(tiny_cfg):
    reply = {"findings": ["- one", "2. two", "• three"], "next_checks": ["* four"]}
    n = narrator.narrate(tiny_cfg, _execution(tiny_cfg), client=_Client(reply))
    assert n.findings == "- one\n- two\n- three"
    assert n.next_checks == "- four"


def test_a_list_of_empty_items_is_an_empty_findings_section(tiny_cfg):
    n = narrator.narrate(tiny_cfg, _execution(tiny_cfg),
                         client=_Client({"findings": ["", "   "], "next_checks": ["x"]}))
    assert n.source == "template"
    assert "empty findings" in n.note


def test_a_list_of_lead_ins_is_still_an_announcement(tiny_cfg):
    reply = {"findings": ["Here are the key findings:"], "next_checks": ["x"]}
    n = narrator.narrate(tiny_cfg, _execution(tiny_cfg), client=_Client(reply))
    assert n.source == "template"
    assert "lead-ins" in n.note


def test_the_system_prompt_says_idle_totals_are_not_machine_hours():
    rules = narrator.SYSTEM.lower()
    assert "head-hours" in rules and "event gaps" in rules


class _OllamaClient:
    """Speaks Ollama's /api/chat shape; records the bodies it was sent."""

    def __init__(self, payload=None, raises=None):
        self._payload, self._raises = payload, raises
        self.calls = []

    def chat(self, body):
        self.calls.append(body)
        if self._raises:
            raise self._raises
        return {"message": {"content": json.dumps(self._payload)}}


@pytest.fixture
def local_cfg(tiny_cfg):
    return replace(tiny_cfg, provider="ollama", model="qwen3:14b")


def _local(local_cfg, payload=None, raises=None, question=None):
    ex = _execution(local_cfg)
    if question:
        ex = replace(ex, plan=replace(ex.plan, question=question))
    client = _OllamaClient(payload, raises)
    return ex, client, narrator.narrate(local_cfg, ex, client=client)


def test_a_local_model_answers_above_the_deterministic_findings(local_cfg):
    ex, _, n = _local(local_cfg, {"answer": ["The success rate is 66.6667%."]})
    base = render.summarise(ex)
    assert n.source == "llm"
    assert n.findings.startswith("- **Answer.** The success rate is 66.6667%.\n")
    # every deterministic sentence is still on the page, after the answer
    assert n.findings.endswith(base.findings)
    assert n.next_checks == base.next_checks


def test_a_local_model_is_given_the_findings_and_the_question_not_the_raw_json(local_cfg):
    ex, client, _ = _local(local_cfg, {"answer": ["ok"]},
                           question="Did the machine get worse?")
    body = client.calls[0]
    system, user = body["messages"][0]["content"], body["messages"][1]["content"]
    assert system == narrator.ANSWER_SYSTEM
    assert "<question>\nDid the machine get worse?\n</question>" in user
    figures = ("Figures drawn in this report: success rate per head "
               "(success_rate_per_head.png); capping speed (capping_speed.png).")
    facts = "\n\n".join([render.summarise(ex).findings, render.tables(ex, max_rows=120), figures])
    assert f"<findings>\n{facts}\n</findings>" in user
    assert "capping_operations" not in user          # no raw tool values
    assert body["format"] == narrator._ANSWER_SCHEMA


def test_an_answer_with_a_number_the_findings_lack_is_rejected(local_cfg, caplog):
    with caplog.at_level("WARNING", logger="analytics.agent.narrator"):
        ex, _, n = _local(local_cfg, {"answer": ["The reject rate fell by 39.4%."]})
    assert n.source == "template"
    assert "39.4" in n.note and "not in the findings" in n.note
    assert n.findings == render.summarise(ex).findings
    assert "rejected answer" in caplog.text and "39.4" in caplog.text


def test_numbers_are_compared_by_value_not_by_spelling():
    assert narrator._ungrounded("fell to 0.005%", "rate 0.0050% in 2026-04") is None
    assert narrator._ungrounded("1096 rejects", "1,096 rejected closures") is None
    assert narrator._ungrounded("1 to 3 of 2 heads", "none") is None       # single digits are free
    assert narrator._ungrounded("in 2026-03, 36 heads", "2026-03 ... 36 heads") is None
    assert "12.5" in narrator._ungrounded("it was 12.5", "it was 12")


def test_a_local_answer_that_is_only_a_lead_in_is_rejected(local_cfg):
    _, _, n = _local(local_cfg, {"answer": ["Here is the answer:"]})
    assert n.source == "template"
    assert "lead-ins" in n.note


def test_a_local_answer_that_is_empty_is_rejected(local_cfg):
    _, _, n = _local(local_cfg, {"answer": ["", "  "]})
    assert n.source == "template"
    assert "empty findings" in n.note


def test_a_local_model_that_fails_leaves_the_template(local_cfg):
    ex, _, n = _local(local_cfg, raises=RuntimeError("connection refused"))
    assert n.source == "template"
    assert "connection refused" in n.note
    assert n.findings == render.summarise(ex).findings


def test_a_marker_in_the_first_local_statement_is_not_printed_twice(local_cfg):
    _, _, n = _local(local_cfg, {"answer": ["1. The success rate is 66.6667%.",
                                            "Head 2 is the weakest."]})
    assert n.findings.startswith("- **Answer.** The success rate is 66.6667%.\n"
                                 "  - Head 2 is the weakest.\n")


def test_the_answer_prompt_pins_what_the_model_got_wrong_on_raw_results():
    rules = narrator.ANSWER_SYSTEM
    assert "drifts, or stands out, only where a finding says so" in rules   # 36 heads "drift"
    assert "nothing about drift" in rules
    assert "head-hours" in rules and "Stops" in rules              # idle is not stopped time
    assert "the machine stopped or its data missing" in rules      # a gap is not a certain stop
    assert 'Never write that the machine "was stopped"' in rules
    assert "one string per list item" in rules
    assert "never answered with a bare yes or no" in rules          # "worse" is per measure


def test_the_hosted_path_is_unchanged_by_the_local_one(tiny_cfg):
    client = _Client(_GOOD)
    n = narrator.narrate(tiny_cfg, _execution(tiny_cfg), client=client)
    assert n.source == "llm" and n.findings == "- The machine is healthy."
    assert "<results>" in client.calls[0]["messages"][0]["content"]


def test_a_number_the_operator_wrote_in_the_question_may_be_repeated():
    # "outside 1.5 to 2.5 Nm" is answered with 1.5 and 2.5, which are in the
    # question and not in any finding; it was rejected and the template stood.
    assert narrator._ungrounded("130 readings fall outside 1.5 to 2.5 Nm",
                                "130 outside the torque band",
                                "Are there values outside 1.5 to 2.5 Nm?") is None
    assert narrator._ungrounded("130 readings fall outside 1.6 to 2.5 Nm",
                                "130 outside the torque band",
                                "Are there values outside 1.5 to 2.5 Nm?") is not None


def test_a_rounded_figure_matches_its_source_but_a_different_one_does_not():
    findings = "Throughput. 29,239.2584 pieces/hour"
    assert narrator._ungrounded("29239.26 pieces/hour", findings) is None
    assert narrator._ungrounded("29,239 pieces/hour", findings) is None
    assert narrator._ungrounded("29,240 pieces/hour", findings) is not None
    assert narrator._ungrounded("39.4% fewer", "reject rate 0.0050% to 0.0011%") is not None


def test_a_local_answer_repeating_the_questions_number_is_kept(local_cfg):
    ex, _, n = _local(local_cfg, {"answer": ["Readings outside 1.5 to 2.5 Nm are in the table."]},
                      question="Are there values outside 1.5 to 2.5 Nm?")
    assert n.source == "llm"


def test_the_local_model_is_handed_the_tables_with_the_findings(local_cfg):
    ex, client, _ = _local(local_cfg, {"answer": ["ok"]})
    user = client.calls[0]["messages"][1]["content"]
    assert "### Success rate per head (table)" in user
    assert "| 2 | 3 | 1 | 2 | 33.3333% |" in user          # head 2 of the tiny store


def test_the_answer_prompt_no_longer_tells_the_model_to_say_heads_are_alike():
    # The rule "within about 1% of each other, say no head stands out" was
    # repeated in answers it had nothing to do with.
    assert "within about 1%" not in narrator.ANSWER_SYSTEM
    assert "stands out, only where a finding says so" in narrator.ANSWER_SYSTEM


def _big_tables(days):
    from analytics.agent.executor import Execution
    from analytics.agent.plan import Plan, PlanStep
    from analytics.result import ToolResult
    heads = ToolResult.ok("success_rates", [
        {"head_id": h, "total": 879000 + h, "successful": 878900, "failed": 100 + h,
         "success_rate": 0.99981234} for h in range(1, 37)])
    daily = ToolResult.ok("success_rates", [
        {"day": f"2026-{2 + d // 31:02d}-{d % 28 + 1:02d}", "total": 400000 + d,
         "successful": 399990, "failed": 10 + d % 7, "success_rate": 0.99997} for d in range(days)])
    steps = [PlanStep("success_rates", {"by": "head"}), PlanStep("success_rates", {"by": "day"})]
    return Execution(plan=Plan(goal="g", steps=steps, question="q"), results=[heads, daily])


def test_a_small_context_cuts_the_long_daily_table_and_keeps_every_head(local_cfg):
    from analytics.report import render as r
    cfg = replace(local_cfg, num_ctx=6144)
    ex = _big_tables(89)
    facts = narrator._facts(cfg, ex, r.summarise(ex))
    assert "| 36 |" in facts                      # all 36 heads reach the model
    assert "more rows not shown" in facts         # the 89 days did not
    tokens = len(facts) / narrator._TABLE_CHARS_PER_TOKEN
    assert tokens < cfg.num_ctx - narrator._REPLY_TOKENS


def test_a_roomy_context_keeps_every_row(local_cfg):
    from analytics.report import render as r
    cfg = replace(local_cfg, num_ctx=32768)
    ex = _big_tables(89)
    facts = narrator._facts(cfg, ex, r.summarise(ex))
    assert "more rows not shown" not in facts
    assert facts.count("| 2026-") >= 89


def test_the_answer_prompt_tells_the_model_to_own_up_to_a_cut_table():
    assert "rows are not shown" in narrator.ANSWER_SYSTEM


def test_the_local_model_is_told_which_figures_the_report_draws(local_cfg):
    _, client, _ = _local(local_cfg, {"answer": ["ok"]})
    user = client.calls[0]["messages"][1]["content"]
    assert ("Figures drawn in this report: success rate per head "
            "(success_rate_per_head.png); capping speed (capping_speed.png).") in user
    assert "Figures drawn in this report" in narrator.ANSWER_SYSTEM
    assert "no figure drawn here shows it" in narrator.ANSWER_SYSTEM
