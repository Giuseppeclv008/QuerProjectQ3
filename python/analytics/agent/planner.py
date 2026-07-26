"""Interpret a question as a plan. The model chooses tools; it computes nothing.

The model is constrained three ways, so a bad plan degrades instead of breaking:

  1. Structured outputs pin the response to the plan schema, so it is JSON with
     the right shape or the API rejects it.
  2. Every step is validated against the registry, so a tool name the model
     invented, or an argument a tool does not take, is caught before execution.
  3. Any failure at all -- no API key, no network, a rate limit, a refusal,
     unparseable JSON, an invalid step -- falls back to the keyword router, and
     the report's limits section says so.

The numbers are identical on every one of those paths, because the tools produce
them.
"""
import json
import logging
from dataclasses import replace

from analytics.agent import llm
from analytics.agent.llm import client as _client
from analytics.agent.plan import Plan, PlanStep, effective_args
from analytics.agent.registry import plan_json_schema, tool_schemas, validate_step
from analytics.agent.router import route

log = logging.getLogger(__name__)

SYSTEM = """You plan analyses of AROL capping-machine telemetry. You do not \
compute or estimate any number: you choose which deterministic tools to run and \
in what order, and the system runs them.

Rules:
- Use only the tools listed. Never invent a tool name or an argument.
- Set `period` on every step to the period you were given, unless the question \
explicitly asks about a different one.
- Leave an argument null to accept that tool's default.
- Prefer the fewest steps that fully answer the question. Two to five is typical.
- Every step needs a one-line rationale naming what it contributes to the answer.
- If the question is about how something changed over time, plan a trend step. \
If it is about which head is unusual, plan head_correlation. If it is about \
whether values are out of range, plan anomalies. If it is about how much or how \
often, plan overview and success_rates."""


def _fallback(question, period, note):
    log.warning("falling back to the keyword router: %s", note)
    routed = route(question, period)
    return Plan(goal=routed.goal, steps=routed.steps, source="router",
                note=f"{note}; {routed.note}")


def plan(cfg, question, period, client=None):
    """A Plan for `question`. Never raises; degrades to the router."""
    client = client or _client(cfg)
    if client is None:
        return _fallback(question, period,
                         "no Anthropic client (missing SDK or ANTHROPIC_API_KEY)")

    tools = json.dumps(tool_schemas(), indent=2)
    prompt = (
        f"Available tools:\n{tools}\n\n"
        f"Period under analysis: {period!r} "
        f"(pass this as the `period` argument).\n\n"
        f"User question: {question}"
    )
    payload, reason = llm.json_call(cfg, client, SYSTEM, prompt, plan_json_schema())
    if payload is None:
        return _fallback(question, period, f"planning failed: {reason}")

    try:
        steps = [PlanStep(tool=s["tool"], args=dict(s["args"]),
                          rationale=s.get("rationale", ""))
                 for s in payload["steps"]]
    except Exception as exc:                       # noqa: BLE001
        return _fallback(question, period,
                         f"the model's plan was not shaped like a plan ({exc})")

    if not steps:
        return _fallback(question, period, "the model returned an empty plan")
    for step in steps:
        # Validate what the executor will actually call, not the null-padded
        # shape the schema forces the model to emit.
        reason = validate_step(replace(step, args=effective_args(step)))
        if reason is not None:
            return _fallback(question, period, f"the model's plan was invalid: {reason}")

    log.info("model planned %d step(s): %s", len(steps), [s.tool for s in steps])
    return Plan(goal=payload.get("goal") or question, steps=steps, source="llm")
