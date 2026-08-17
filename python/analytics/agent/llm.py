"""The one place this project talks to a model.

Two callers -- the planner and the narrator -- ask for a single JSON object
constrained by a schema, and both must degrade rather than raise when anything
goes wrong. Keeping the request here means a provider's quirks are confined to
one file: the parameters Anthropic rejects with a 400 (temperature, top_p,
top_k, budget_tokens) can only be got wrong once, and Ollama never sees the
Anthropic-only fields at all.

Switching between a hosted model and one running on the machine is `provider` in
the config and nothing else. Both return the same (payload, reason) pair, so the
planner and the narrator cannot tell which one answered -- and neither can the
report, beyond what it discloses about the model's name.

Failures are returned, not raised, because both callers already have something
honest to fall back to and the reason belongs in the report rather than in a
traceback.
"""
import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)


# --------------------------------------------------------------- anthropic
def _anthropic_client(cfg):
    import anthropic
    return anthropic.Anthropic(timeout=cfg.api_timeout_s)


def _anthropic_call(cfg, client, system, prompt, schema):
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
    if getattr(response, "stop_reason", None) == "max_tokens":
        # Undetected, this surfaced downstream as "the reply was not valid
        # JSON" -- blaming the model for a budget the config set.
        return None, (f"the reply was cut off at max_tokens={cfg.max_tokens}; "
                      "raise it in the config")
    text = next((b.text for b in response.content
                 if getattr(b, "type", "") == "text"), None)
    if text is None:
        return None, "the reply carried no text block"
    return _parse(text)


# ------------------------------------------------------------------ ollama
class OllamaClient:
    """A minimal Ollama chat client.

    Deliberately stdlib-only. Ollama's chat endpoint is one POST, and the
    `ollama` package would add a dependency for a provider a user may never
    enable -- while `anthropic` is already required for the default path.
    """

    def __init__(self, host, timeout):
        self.host, self.timeout = host.rstrip("/"), timeout

    def chat(self, body):
        request = urllib.request.Request(
            f"{self.host}/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))


def _ollama_client(cfg):
    client = OllamaClient(cfg.ollama_host, cfg.api_timeout_s)
    # Fail here rather than at call time, so "the daemon is not running" is
    # reported once by the planner instead of twice more by the narrator.
    with urllib.request.urlopen(f"{client.host}/api/version", timeout=5):
        pass
    return client


def _ollama_call(cfg, client, system, prompt, schema):
    try:
        response = client.chat({
            "model": cfg.model,
            "stream": False,
            # Ollama constrains generation to the schema via a grammar. It does
            # not accept Anthropic's output_config/thinking, so they are absent.
            "format": schema,
            "options": {"num_ctx": cfg.num_ctx, "temperature": 0},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
        })
    except Exception as exc:                       # noqa: BLE001
        return None, f"the call failed: {exc}"

    text = (response.get("message") or {}).get("content")
    if not text:
        return None, "the reply carried no content"
    return _parse(text)


# ------------------------------------------------------------------ shared
_CLIENTS = {"anthropic": _anthropic_client, "ollama": _ollama_client}
_CALLS = {"anthropic": _anthropic_call, "ollama": _ollama_call}


def _parse(text):
    try:
        payload = json.loads(text)
    except Exception as exc:                       # noqa: BLE001
        return None, f"the reply was not valid JSON ({exc})"
    # Both callers index the payload as an object, and two of the planner's
    # three tiers did it without asking -- so `[]`, `42` or a bare string left
    # this function as a valid payload and arrived as an AttributeError. `null`
    # was worse: it parsed, and (None, None) read downstream as a failure with
    # no reason, printing "planning failed: None". One check, one honest reason.
    if not isinstance(payload, dict):
        kind = "null" if payload is None else type(payload).__name__
        return None, f"the reply was JSON but not an object ({kind})"
    return payload, None


def client(cfg):
    """A client for the configured provider, or None if it is unreachable."""
    try:
        return _CLIENTS[cfg.provider](cfg)
    except Exception as exc:                       # noqa: BLE001
        log.info("no %s client available (%s)", cfg.provider, exc)
        return None


def json_call(cfg, client, system, prompt, schema):
    """Ask for one JSON object. Returns (payload, None) or (None, reason)."""
    return _CALLS[cfg.provider](cfg, client, system, prompt, schema)
