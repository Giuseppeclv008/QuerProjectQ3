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
from dataclasses import asdict

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


def _payload(execution):
    return json.dumps([
        {
            "tool": r.tool,
            "status": r.status,
            "message": r.message,
            "values": r.values,
            "provenance": asdict(r.provenance),
        }
        for r in execution.results
    ], indent=2, default=str)


def narrate(cfg, execution, client=None):
    """Model-written Findings and Next checks, or the deterministic summary."""
    client = client or _client(cfg)
    if client is None:
        return summarise(execution)

    prompt = (f"Goal: {execution.plan.goal}\n\n"
              f"Results:\n{_payload(execution)}")
    payload, reason = llm.json_call(cfg, client, SYSTEM, prompt, _SCHEMA)
    if payload is None:
        log.warning("narration fell back to the deterministic summary: %s", reason)
        return summarise(execution)
    try:
        return Narrative(findings=payload["findings"],
                         next_checks=payload["next_checks"], source="llm")
    except Exception as exc:                       # noqa: BLE001
        log.warning("narration fell back to the deterministic summary: %s", exc)
        return summarise(execution)
