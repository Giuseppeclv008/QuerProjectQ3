"""Turn results into prose. The numbers arrive fixed and leave fixed.

The model is given the tool results verbatim and asked to write the Findings and
Next checks sections around them. It cannot compute anything: it has no store
access, and the figures, the trace, and the limits section are all rendered from
the ToolResults regardless of what it writes. A narrator failure therefore costs
readability, never correctness -- which is why the fallback is the deterministic
summary rather than an error.
"""
import json
import logging
from dataclasses import asdict, replace

from analytics.agent import llm
from analytics.agent.llm import client as _client
from analytics.report.render import Narrative, summarise

log = logging.getLogger(__name__)

SYSTEM = """You write the Findings and Next checks sections of a technical report \
on AROL capping-machine telemetry, for engineers in R&D and Service.

You are given the exact output of deterministic analyses. Rules:
- Never state a number that is not present in the results you were given, and \
never round one into a different claim.
- Findings: Markdown bullets. Lead with what matters operationally. Name heads, \
periods, and magnitudes. If a result says insufficient_data or error, say the \
analysis could not answer rather than inferring anything.
- Next checks: Markdown bullets. Concrete, actionable, and tied to a finding \
above. No generic advice.
- Be direct. No preamble, no restating the question, no hedging."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {"type": "string", "description": "Markdown bullet list."},
        "next_checks": {"type": "string", "description": "Markdown bullet list."},
    },
    "required": ["findings", "next_checks"],
    "additionalProperties": False,
}


# A tool's list-valued results are either an analytic grouping -- bounded by the
# head count (36 here, 48 on the brief's example machine) or by the days in a
# period -- or a hit list, which is bounded by nothing. On the real three-month
# store `anomalies` returns 678,325 deviation hits and `idle_periods` returns
# 12,276 periods. Serialising those in full builds a multi-megabyte prompt that
# no request can carry, so `ask` would fail on every question about real data.
# This cap passes every grouping through whole and truncates only the hit lists,
# which is what the narrator needs: the tools already return their counts
# alongside, and the narrator is forbidden to compute anything from the items.
_MAX_ITEMS = 120


def _bounded(value):
    """`value` with long lists replaced by their length and a sample."""
    if isinstance(value, list) and len(value) > _MAX_ITEMS:
        return {"item_count": len(value),
                "note": f"list truncated to the first {_MAX_ITEMS} of {len(value)}",
                "sample": [_bounded(v) for v in value[:_MAX_ITEMS]]}
    if isinstance(value, list):
        return [_bounded(v) for v in value]
    if isinstance(value, dict):
        return {k: _bounded(v) for k, v in value.items()}
    return value


def _payload(execution):
    return json.dumps([
        {
            "tool": r.tool,
            "status": r.status,
            "message": r.message,
            "values": _bounded(r.values),
            "provenance": asdict(r.provenance),
        }
        for r in execution.results
    ], indent=2, default=str)


def _fallback(execution, reason):
    """The deterministic summary, carrying why the model did not write it.

    The reason has to reach the report. Without it a run shows `plan source: llm,
    narrative source: template` and says nothing about what went wrong -- the
    planner's fallback reason is disclosed in the limits section and the
    narrator's was not.
    """
    log.warning("narration fell back to the deterministic summary: %s", reason)
    return replace(summarise(execution), note=reason)


def narrate(cfg, execution, client=None):
    """Model-written Findings and Next checks, or the deterministic summary."""
    client = client or _client(cfg)
    if client is None:
        return _fallback(execution, "no Anthropic client (missing SDK or "
                                    "ANTHROPIC_API_KEY)")

    prompt = (f"Goal: {execution.plan.goal}\n\n"
              f"Results:\n{_payload(execution)}")
    payload, reason = llm.json_call(cfg, client, SYSTEM, prompt, _SCHEMA)
    if payload is None:
        return _fallback(execution, reason)
    try:
        return Narrative(findings=payload["findings"],
                         next_checks=payload["next_checks"], source="llm")
    except Exception as exc:                       # noqa: BLE001
        return _fallback(execution, f"the model's reply was missing a section ({exc})")
