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


def _client(cfg):
    """The Anthropic client, or None if the SDK or credentials are absent."""
    try:
        import anthropic
        return anthropic.Anthropic(timeout=cfg.api_timeout_s)
    except Exception as exc:                       # noqa: BLE001
        log.info("no Anthropic client available (%s)", exc)
        return None


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
    try:
        response = client.messages.create(
            model=cfg.model,
            max_tokens=cfg.max_tokens,
            thinking={"type": "adaptive"},
            output_config={
                "effort": cfg.effort,
                "format": {"type": "json_schema", "schema": plan_json_schema()},
            },
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:                       # noqa: BLE001
        return _fallback(question, period, f"the planning call failed: {exc}")

    if getattr(response, "stop_reason", None) == "refusal":
        return _fallback(question, period, "the model refused the planning request")

    text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), "")
    try:
        payload = json.loads(text)
        steps = [PlanStep(tool=s["tool"], args=dict(s["args"]),
                          rationale=s.get("rationale", ""))
                 for s in payload["steps"]]
    except Exception as exc:                       # noqa: BLE001
        return _fallback(question, period,
                         f"the model's plan was not valid JSON ({exc})")

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
