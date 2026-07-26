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
from analytics.agent.registry import (
    TOOLS, plan_json_schema, tool_schemas, validate_step,
)
from analytics.agent.router import REPORT_TYPES, canned_plan, route

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


CLASSIFY_SYSTEM = """Pick the report that best answers the user's question.

- kpi: how much, how many, how fast, success rates, idle time, throughput.
- drift: change over time, a head behaving differently from the others, \
correlation between heads, torque walking.
- anomalies: values out of range, rejected caps, outliers, faults."""

SELECT_SYSTEM = """Choose which analyses to run to answer the user's question.

You do not compute anything: the system runs the tools you name and produces
every number. Use only the tools listed. Prefer the fewest that fully answer the
question; two to four is typical."""


def _schema_style(cfg):
    """Anthropic structured outputs require every property in `required`, which
    forces the flat union. Anywhere else, the per-tool shape is better."""
    return "flat" if cfg.provider == "anthropic" else "per_tool"


def _classify(cfg, client, question, period):
    """Tier 1: the model picks a report type; the canned plan does the rest.

    One word of output against a prompt of a few dozen tokens. A model far too
    small to compose a plan can still route reliably, and this beats the keyword
    router on any phrasing the keywords do not literally contain.
    """
    schema = {"type": "object",
              "properties": {"report": {"type": "string",
                                        "enum": list(REPORT_TYPES)}},
              "required": ["report"],
              "additionalProperties": False}
    payload, reason = llm.json_call(cfg, client, CLASSIFY_SYSTEM, question, schema)
    if payload is None:
        return _fallback(question, period, f"planning failed: {reason}")
    choice = payload.get("report")
    if choice not in REPORT_TYPES:
        return _fallback(question, period,
                         f"the model chose {choice!r}, which is not a report type")
    routed = canned_plan(choice, period)
    log.info("model classified the question as %r", choice)
    return Plan(goal=routed.goal, steps=routed.steps, source="llm",
                note=f"the model chose the {choice} report; its fixed plan was run")


def _select(cfg, client, question, period):
    """Tier 2: the model picks the tools; their own defaults supply the arguments.

    Keeps real composition -- which analyses, in what order -- without asking a
    small model to fill an arguments object it will get wrong.
    """
    catalogue = "\n".join(f"- {name}: {spec.description}"
                          for name, spec in sorted(TOOLS.items()))
    schema = {"type": "object",
              "properties": {"goal": {"type": "string"},
                             "tools": {"type": "array",
                                       "items": {"type": "string",
                                                 "enum": sorted(TOOLS)}}},
              "required": ["goal", "tools"],
              "additionalProperties": False}
    prompt = f"Available tools:\n{catalogue}\n\nUser question: {question}"
    payload, reason = llm.json_call(cfg, client, SELECT_SYSTEM, prompt, schema)
    if payload is None:
        return _fallback(question, period, f"planning failed: {reason}")

    names = payload.get("tools") or []
    unknown = [n for n in names if n not in TOOLS]
    if unknown:
        return _fallback(question, period, f"the model named unknown tools: {unknown}")
    if not names:
        return _fallback(question, period, "the model selected no tools")

    steps = [PlanStep(tool=n, args={"period": period},
                      rationale="selected by the model; arguments left at their defaults")
             for n in names]
    log.info("model selected %d tool(s): %s", len(steps), names)
    return Plan(goal=payload.get("goal") or question, steps=steps, source="llm",
                note="the model chose the tools; their arguments are the defaults")


def _plan(cfg, client, question, period):
    """Tier 3: the model composes the whole sequence, arguments included."""
    tools = json.dumps(tool_schemas(), indent=2)
    prompt = (
        f"Available tools:\n{tools}\n\n"
        f"Period under analysis: {period!r} "
        f"(pass this as the `period` argument).\n\n"
        f"User question: {question}"
    )
    schema = plan_json_schema(_schema_style(cfg))
    payload, reason = llm.json_call(cfg, client, SYSTEM, prompt, schema)
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
        # shape the flat schema forces the model to emit.
        reason = validate_step(replace(step, args=effective_args(step)))
        if reason is not None:
            return _fallback(question, period, f"the model's plan was invalid: {reason}")

    log.info("model planned %d step(s): %s", len(steps), [s.tool for s in steps])
    return Plan(goal=payload.get("goal") or question, steps=steps, source="llm")


_TIERS = {"classify": _classify, "select": _select, "plan": _plan}


def plan(cfg, question, period, client=None):
    """A Plan for `question`. Never raises; degrades to the router."""
    client = client or _client(cfg)
    if client is None:
        return _fallback(question, period,
                         f"no {cfg.provider} client (unreachable, or missing SDK "
                         f"or credentials)")
    return _TIERS[cfg.planning](cfg, client, question, period)
