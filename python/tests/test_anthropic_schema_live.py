"""Does the Anthropic structured-output API accept the plan schemas we send?

The one gap `docs/validation-log.md` has carried open since 2026-08-16. The flat
schema ships JSON Schema keywords the structured-output API does not support,
and nothing strips them: `registry.plan_json_schema` is handed straight to
`output_config["format"]["schema"]`, so it reaches the API verbatim. The SDK's
constraint-stripping only applies when it builds a schema from a Pydantic
model, which is not this path.

Every other test of this tier runs offline against a fake transport, which is
why the gap survived: no test could distinguish "our schema is valid" from
"nothing ever sent it". This one sends it, and is gated on a key being present
so the default suite still runs with no network and no spend.

Cost: a rejected schema is a 400 at request validation -- no tokens are
generated and nothing is billed. An accepted one costs a fraction of a cent on
Haiku at max_tokens=64. Schema acceptance is validated API-side and does not
depend on the model, so there is no reason to spend Opus tokens here.
"""
import os

import pytest

from analytics.agent.registry import plan_json_schema

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY unset; this test spends from the account",
)

# Cheapest model that supports structured outputs. The question under test is
# whether the *schema* is accepted, which the API validates before any model
# runs -- so the model choice changes the price and nothing else.
MODEL = "claude-haiku-4-5"


def _probe(style):
    """Send the schema and report (accepted, detail). Never raises."""
    import anthropic

    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model=MODEL,
            # max_tokens=0 is the free pre-warm trick, but the API rejects it
            # alongside output_config.format -- 64 is about as low as this goes.
            max_tokens=64,
            output_config={"format": {"type": "json_schema",
                                      "schema": plan_json_schema(style)}},
            messages=[{"role": "user",
                       "content": "Return the smallest valid plan."}],
        )
        return True, response
    except anthropic.BadRequestError as exc:
        return False, exc.message
    except anthropic.AuthenticationError as exc:
        # A bad or expired key is a configuration problem, not a verdict on the
        # schema. Skipping rather than failing keeps the two apart: a red
        # "the flat schema was rejected" caused by a typo'd key would be the
        # same class of wrong-reason failure this suite exists to prevent.
        pytest.skip(f"ANTHROPIC_API_KEY is set but not valid: {exc.message}")
    except anthropic.PermissionDeniedError as exc:
        pytest.skip(f"key lacks access to {MODEL}: {exc.message}")
    except anthropic.RateLimitError as exc:
        pytest.skip(f"rate limited before the schema was judged: {exc.message}")


def test_the_per_tool_schema_is_accepted():
    """The shape every non-Anthropic provider gets, and the fallback if the
    flat one is unusable. If this fails, the gap is wider than the log
    described: no provider would be getting a usable schema."""
    accepted, detail = _probe("per_tool")
    assert accepted, f"the per-tool schema was rejected: {detail}"


def test_the_flat_schema_is_accepted():
    """The Anthropic-only shape, and the actual subject of the open gap.

    `planner._schema_style` returns "flat" for provider == "anthropic" and
    "per_tool" for everything else, so this is the only schema a real Anthropic
    run would send -- and the only one never validated against the real API.

    A failure here is the gap closing as a confirmed defect, not a flake: the
    fix is to drop the unsupported keywords (`minimum`, `maxItems`) from the
    flat schema, or to route Anthropic through the per-tool shape as well.
    """
    accepted, detail = _probe("flat")
    assert accepted, (
        "the flat schema was rejected by the structured-output API: "
        f"{detail}\n"
        "This is the open gap in docs/validation-log.md confirming itself. "
        "The unsupported keywords in registry.plan_json_schema('flat') are "
        "`minimum` and `maxItems`; the API strips neither, because the schema "
        "reaches it verbatim."
    )
