"""The one place this project talks to the model.

Two callers -- the planner and the narrator -- ask for a single JSON object
constrained by a schema, and both must degrade rather than raise when anything
goes wrong. Keeping the request in one function means the parameters this model
rejects with a 400 (temperature, top_p, top_k, budget_tokens) can only be got
wrong in one place, and a second caller cannot quietly reintroduce one.

Failures are returned, not raised, because both callers already have something
honest to fall back to and the reason belongs in the report rather than in a
traceback.
"""
import json
import logging

log = logging.getLogger(__name__)


def client(cfg):
    """The Anthropic client, or None if the SDK or credentials are absent."""
    try:
        import anthropic
        return anthropic.Anthropic(timeout=cfg.api_timeout_s)
    except Exception as exc:                       # noqa: BLE001
        log.info("no Anthropic client available (%s)", exc)
        return None


def json_call(cfg, client, system, prompt, schema):
    """Ask for one JSON object. Returns (payload, None) or (None, reason)."""
    try:
        response = client.messages.create(
            model=cfg.model,
            max_tokens=cfg.max_tokens,
            thinking={"type": "adaptive"},
            output_config={"effort": cfg.effort,
                           "format": {"type": "json_schema", "schema": schema}},
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:                       # noqa: BLE001
        return None, f"the call failed: {exc}"

    if getattr(response, "stop_reason", None) == "refusal":
        return None, "the model refused the request"
    try:
        text = next(b.text for b in response.content
                    if getattr(b, "type", "") == "text")
        return json.loads(text), None
    except Exception as exc:                       # noqa: BLE001
        return None, f"the reply was not valid JSON ({exc})"
