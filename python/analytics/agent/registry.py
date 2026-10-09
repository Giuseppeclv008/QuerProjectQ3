"""The tool contract, in one place.

The same entries do three jobs: they generate the schema the model plans against,
they validate what it plans, and they dispatch the call. That is deliberate -- a
tool the model can name is by construction a tool that exists with those exact
arguments, and adding a WP2 tool without registering it fails a test rather than
silently making it unreachable.
"""
from dataclasses import dataclass

from analytics.tools.anomaly import anomalies
from analytics.tools.compare import compare_periods
from analytics.tools.correlation import head_correlation
from analytics.tools.factors import failure_correlation
from analytics.tools.filter import closure_filter
from analytics.tools.gaps import event_gaps
from analytics.tools.idle import idle_periods
from analytics.tools.methodology import TOPICS, methodology
from analytics.tools.overview import overview
from analytics.tools.speed import capping_speed
from analytics.tools.success import success_rates
from analytics.tools.torque import torque_stats
from analytics.tools.trend import trend

_PERIOD = {
    "type": ["string", "null"],
    "description": "'YYYY-MM', or 'YYYY-MM..YYYY-MM' for a range. null = whole store.",
}


def _enum(values, description, nullable=True):
    return {
        "type": ["string", "null"] if nullable else "string",
        "enum": list(values) + ([None] if nullable else []),
        "description": description,
    }


@dataclass(frozen=True)
class ToolSpec:
    name: str
    fn: object
    description: str
    params: dict          # param name -> JSON-schema fragment
    # What the tool does NOT measure, in one sentence. Shown to the planner
    # beside the description and printed in the report whenever the tool ran,
    # so a reader cannot take an answer for more than it is.
    not_measured: str = ""


TOOLS = {
    spec.name: spec
    for spec in [
        ToolSpec(
            "overview", overview,
            "Dataset exploration and WP1 validation: how many capping operations, "
            "how many succeeded/failed/no-load, which heads fired, the time range "
            "covered, and counts of null torque, out-of-band torque, and counter resets.",
            {"period": _PERIOD},
            "Not a rate or a trend: it counts the period as a whole, and says "
            "nothing about when within it anything happened.",
        ),
        ToolSpec(
            "success_rates", success_rates,
            "Success rate = successful / (successful + failed), grouped per head, "
            "per day, or overall. Answers 'what percentage succeeded', 'which head "
            "is lowest', 'show a daily breakdown'.",
            {"period": _PERIOD,
             "by": _enum(["head", "day", "overall"], "grouping; default 'head'")},
            "Not why a closure was rejected, nor production volume: no-load "
            "cycles and closures without a verdict are outside the rate.",
        ),
        ToolSpec(
            "torque_stats", torque_stats,
            "Closing-torque statistics (n, mean, min, max, stddev, median) for "
            "successful, failed, or all closures, optionally per head. Answers "
            "'average torque', 'distribution', 'which head is most variable'.",
            {"period": _PERIOD,
             "outcome": _enum(["successful", "failed", "all"],
                              "which closures to measure; default 'successful'"),
             "by": _enum(["head"], "set to 'head' for per-head breakdown; null = overall")},
            "Not change over time (that is trend), not whether a closure passed, "
            "and not how many readings fall outside the band (anomalies counts "
            "those): a statistic over the whole period.",
        ),
        ToolSpec(
            "capping_speed", capping_speed,
            "Production rate in pieces/hour, bucketed by hour or day, plus the mean "
            "over active buckets. Answers 'how fast is the machine running'.",
            {"period": _PERIOD,
             "bucket": _enum(["hour", "day"], "bucket size; default 'hour'")},
            "Not downtime: hours with no closure are skipped, so the rate is "
            "how fast the machine ran while it ran.",
        ),
        ToolSpec(
            "idle_periods", idle_periods,
            "Sustained runs of No-Load cycles per head, longer than a threshold: "
            "the head cycles but applies no cap. NOT machine downtime -- a stopped "
            "machine advances no counter, so it emits no events at all and cannot "
            "appear here. Answers 'how much no-load cycling', 'which head idles'.",
            {"period": _PERIOD,
             "min_seconds": {"type": ["integer", "null"], "minimum": 1,
                             "description": "minimum run length in seconds; null = config default"}},
            "Not machine downtime: a stopped machine emits no events and "
            "cannot appear here (event_gaps measures that).",
        ),
        ToolSpec(
            "anomalies", anomalies,
            "Deterministic anomaly detection: rejected closures, torque outside the "
            "configured operating band, and per-head robust deviation (median +/- k*MAD). "
            "Counts the out-of-band readings per head, most first. Answers 'any "
            "torque outside the expected range', 'which heads have the most "
            "out-of-band readings', 'abnormal intervals'.",
            {"period": _PERIOD,
             "method": _enum(["threshold", "deviation", "both"],
                             "detection method; default 'both'")},
            "Not causes: the data holds no operator, cap lot, supplier or "
            "maintenance record, so a flag says when and where, never why.",
        ),
        ToolSpec(
            "trend", trend,
            "Moving averages and drift detection on a per-head time series. Signal is "
            "torque or success_rate; drift is Mann-Kendall tau. Answers 'did torque "
            "change over the month', 'how did success evolve', 'which head is drifting'.",
            {"period": _PERIOD,
             "signal": _enum(["torque", "success_rate"], "series to analyse; default 'torque'"),
             "by": _enum(["day", "hour"], "bucket size; default 'day'"),
             "window": {"type": ["integer", "null"], "minimum": 1,
                        "description": "rolling window in buckets; null = 7"}},
            "Not production volume or downtime: a drift is a monotone trend in "
            "one head's torque or success rate.",
        ),
        ToolSpec(
            "head_correlation", head_correlation,
            "Pairwise correlation of per-head bucketed torque series, plus a ranking "
            "of heads by mean correlation to their peers. Answers 'compare head 1 and "
            "head 5', 'which head behaves differently from the others'.",
            {"period": _PERIOD,
             "heads": {"type": ["array", "null"], "maxItems": 64,
                       "items": {"type": "integer", "minimum": 1},
                       "description": "heads to compare; null = every head in the store"},
             "by": _enum(["day", "hour"], "bucket size; default 'day'")},
            "Not level: correlation ignores a per-head offset, so a head "
            "running steadily lower than the rest scores as normal.",
        ),
        ToolSpec(
            "event_gaps", event_gaps,
            "Machine-wide holes in the event stream: stretches with no event "
            "from any head, longer than a threshold. The closest measure of "
            "downtime the store holds -- total hours, count, and the longest. "
            "Answers 'how long was the machine stopped', 'when did it stop'.",
            {"period": _PERIOD,
             "min_seconds": {"type": ["integer", "null"], "minimum": 1,
                             "description": "shortest gap counted, in seconds; "
                                            "null = config default (600)"}},
            "Not the cause of a stop, and not which head: silence is the "
            "machine stopped or its data missing, and the two look the same.",
        ),
        ToolSpec(
            "compare_periods", compare_periods,
            "Month-by-month or week-by-week comparison of the whole machine: "
            "capping operations, caps per day, rejects and reject rate, no-load "
            "cycles and their share, active days, the torque mean and spread, and "
            "the change from the first bucket to the last. Answers 'did it get "
            "worse', 'compare March and April'.",
            {"period": _PERIOD,
             "by": _enum(["month", "week"], "bucket size; default 'month'")},
            "Not per head and not why a period differs: it compares totals.",
        ),
        ToolSpec(
            "closure_filter", closure_filter,
            "Counts, exactly, the capping operations that match a filter the "
            "operator names -- torque above and/or below a value in Nm, one "
            "outcome, one head -- with the count per head and the first events "
            "listed. Answers 'how many closures had torque above X Nm', 'list the "
            "failed events below a threshold', 'all failed events of head 3'.",
            {"period": _PERIOD,
             "above": {"type": ["number", "null"],
                       "description": "torque strictly above this value, Nm; null = no limit"},
             "below": {"type": ["number", "null"],
                       "description": "torque strictly below this value, Nm; null = no limit"},
             "outcome": _enum(["successful", "failed", "all"],
                              "which closures; default 'all'"),
             "head": {"type": ["integer", "null"], "minimum": 1,
                      "description": "one head only; null = every head"}},
            "Not the configured band (that is anomalies) and not why a closure "
            "has its torque: it counts against the values given, and lists no "
            "more than the first 20 events.",
        ),
        ToolSpec(
            "failure_correlation", failure_correlation,
            "How the chance of a rejection varies with a factor: the hour of the "
            "day (the reject rate of each hour, and a chi-square test of whether it "
            "depends on the hour) or the closing torque (the correlation between "
            "torque and success, and the reject rate below, inside and above the "
            "torque band). Answers 'is there a correlation between time of day and "
            "failure probability', 'does higher torque correlate with higher "
            "success rate'.",
            {"period": _PERIOD,
             "by": _enum(["hour_of_day", "torque"], "the factor; default 'hour_of_day'")},
            "Not a cause: a rate that varies with the hour or the torque says when "
            "and where closures fail, not why, and the hour is the one the store "
            "holds, with no time-zone conversion.",
        ),
        ToolSpec(
            "methodology", methodology,
            "How the data was prepared and a closure is judged, as documented: "
            "preprocessing of the raw rows, how duplicated closures are avoided, "
            "the assumptions of the cleaning, and what classifies a closure as "
            "successful or rejected. Answers 'what preprocessing was applied', "
            "'how were duplicates removed', 'which assumptions were made', 'what "
            "classifies a successful closure'.",
            {"topic": _enum(list(TOPICS), "one topic; null = all four")},
            "Not a measurement of the store: it restates the pipeline's rules "
            "and says nothing about how often one applied.",
        ),
    ]
}

def _merge_params(specs):
    """Union every tool's arguments into one flat schema for the plan.

    Structured outputs need a single closed object, but several tools share an
    argument NAME with a different set of legal values -- `by` is
    head|day|overall for success_rates, head for torque_stats, and day|hour for
    trend and head_correlation. A plain dict update would let the last tool win
    and make `by="head"` structurally unemittable, so the enums are UNIONED here.

    That makes the schema deliberately permissive: it constrains shape, not
    per-tool legality. `validate_step()` is the strict gate -- it checks each
    argument against the enum of the tool actually named, so a model that plans
    success_rates(by="hour") is rejected there and the request falls back to the
    router. Schema permissive, validation strict; never the other way round.
    """
    merged = {}
    for spec in specs:
        for name, schema in spec.params.items():
            if name not in merged:
                merged[name] = dict(schema)
                continue
            known, incoming = merged[name].get("enum"), schema.get("enum")
            if known and incoming:
                # Preserve order, drop duplicates, keep None last.
                values = [v for v in known if v is not None]
                values += [v for v in incoming if v is not None and v not in values]
                merged[name]["enum"] = values + [None]
                merged[name]["description"] = "varies by tool; see the tool list"
    return merged


# Every argument any tool accepts, unioned. The executor drops nulls; the
# registry rejects arguments and values the named tool does not accept.
_ALL_ARGS = _merge_params(TOOLS.values())


def tool_schemas():
    """Anthropic tool definitions, one per WP2 tool."""
    return [
        {
            "name": spec.name,
            "description": f"{spec.description} Does NOT measure: {spec.not_measured}",
            "input_schema": {
                "type": "object",
                "properties": dict(spec.params),
                "required": [],
                "additionalProperties": False,
            },
        }
        for spec in TOOLS.values()
    ]


def _per_tool_plan_schema():
    """One branch per tool: a step's args carry only that tool's parameters."""
    branches = [
        {
            "type": "object",
            "properties": {
                "tool": {"type": "string", "enum": [name]},
                "args": {
                    "type": "object",
                    "properties": {k: dict(v) for k, v in spec.params.items()},
                    "additionalProperties": False,
                },
                "rationale": {
                    "type": "string",
                    "description": "One line: why this call answers the question.",
                },
            },
            "required": ["tool", "args", "rationale"],
            "additionalProperties": False,
        }
        for name, spec in sorted(TOOLS.items())
    ]
    return {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "One sentence restating what the user asked for.",
            },
            "steps": {
                "type": "array",
                "description": "Tool calls to run, in order.",
                "maxItems": 12,
                "items": {"anyOf": branches},
            },
        },
        "required": ["goal", "steps"],
        "additionalProperties": False,
    }


def plan_json_schema(style="flat"):
    """The structured-output schema the planner constrains the model to.

    Two shapes, because the providers do not accept the same one.

    `flat` -- every step's args is a single object carrying the union of every
    tool's parameters, all of them required. This is not a preference: Anthropic
    structured outputs require every property to appear in `required`, so the
    model must spell out arguments its tool does not take and set them to null.
    `plan.effective_args()` drops those nulls again.

    `per_tool` -- one branch per tool, each carrying only that tool's own
    parameters. Strictly better wherever the provider allows it. Measured against
    qwen2.5:7b, the flat shape had 3 of 6 plans rejected by `validate_step` --
    every one because the model attached `outcome` to `trend`, which does not
    take it -- while the per-tool shape had none rejected and generated faster,
    having no nulls to emit. It makes that mistake ungrammatical rather than
    merely invalid.
    """
    if style not in ("flat", "per_tool"):
        raise ValueError(f"style must be 'flat' or 'per_tool', got {style!r}")
    if style == "per_tool":
        return _per_tool_plan_schema()
    return _strip_unsupported_keywords({
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "One sentence restating what the user asked for.",
            },
            "steps": {
                "type": "array",
                "description": "Tool calls to run, in order.",
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string", "enum": sorted(TOOLS)},
                        "args": {
                            "type": "object",
                            "properties": dict(_ALL_ARGS),
                            "required": sorted(_ALL_ARGS),
                            "additionalProperties": False,
                        },
                        "rationale": {
                            "type": "string",
                            "description": "One line: why this call answers the question.",
                        },
                    },
                    "required": ["tool", "args", "rationale"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["goal", "steps"],
        "additionalProperties": False,
    })


# Keywords Anthropic structured outputs reject: numeric constraints and array
# sizes are not in the supported subset (basic types, enum, const,
# anyOf/allOf, $ref, string formats, additionalProperties: false). The SDK
# strips these only on the messages.parse() path; llm.py passes a raw dict
# through output_config["format"]["schema"], so it reaches the API verbatim
# -- and a rejected schema fails every hosted `ask` into the keyword
# router: the hosted path never running. Nothing is lost by stripping:
# validate_step() enforces minimum/maxItems strictly after the model answers.
# Schema permissive, validation strict; never the other way round.
_UNSUPPORTED_KEYWORDS = ("minimum", "maximum", "maxItems", "minItems",
                         "maxLength", "minLength")


def _strip_unsupported_keywords(schema):
    if isinstance(schema, dict):
        return {k: _strip_unsupported_keywords(v)
                for k, v in schema.items() if k not in _UNSUPPORTED_KEYWORDS}
    if isinstance(schema, list):
        return [_strip_unsupported_keywords(v) for v in schema]
    return schema


_JSON_TYPES = {"string": str, "integer": int, "number": (int, float),
               "array": list, "boolean": bool, "object": dict}


def _type_ok(value, schema):
    """Does `value` satisfy the schema fragment's "type" (a name or a list)?"""
    types = schema.get("type")
    if types is None:
        return True
    if isinstance(types, str):
        types = [types]
    if value is None:
        return "null" in types
    if isinstance(value, bool):        # bool is an int subclass; keep it apart
        return "boolean" in types
    return any(isinstance(value, _JSON_TYPES[t])
               for t in types if t in _JSON_TYPES)


def validate_step(step):
    """None if the step is callable as written, else why it is not.

    The strict half of "schema permissive, validation strict": the schema is
    loose so a wobbly model reply still parses, and THIS gate is what keeps a
    mistyped or out-of-range argument from reaching a tool. It checks types,
    minimums, and array bounds against the same fragments the schema is built
    from: without it, trend(window=-5) and head_correlation(heads=[0..99999])
    walk straight through.
    """
    spec = TOOLS.get(step.tool)
    if spec is None:
        return f"unknown tool {step.tool!r}; known tools: {sorted(TOOLS)}"
    for key, value in step.args.items():
        if key not in spec.params:
            return (f"tool {step.tool!r} takes no argument {key!r}; "
                    f"it accepts {sorted(spec.params)}")
        sch = spec.params[key]
        allowed = sch.get("enum")
        if allowed is not None and value not in allowed:
            return (f"{step.tool}.{key} must be one of "
                    f"{[a for a in allowed if a is not None]}, got {value!r}")
        if not _type_ok(value, sch):
            return (f"{step.tool}.{key} must have type {sch.get('type')}, "
                    f"got {type(value).__name__} ({value!r})")
        if value is None:
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            minimum = sch.get("minimum")
            if minimum is not None and value < minimum:
                return f"{step.tool}.{key} must be >= {minimum}, got {value!r}"
        if isinstance(value, list):
            max_items = sch.get("maxItems")
            if max_items is not None and len(value) > max_items:
                return (f"{step.tool}.{key} takes at most {max_items} items, "
                        f"got {len(value)}")
            items = sch.get("items")
            if items is not None:
                for v in value:
                    if not _type_ok(v, items):
                        return (f"{step.tool}.{key} items must have type "
                                f"{items.get('type')}, got {v!r}")
                    imin = items.get("minimum")
                    if (imin is not None and isinstance(v, (int, float))
                            and not isinstance(v, bool) and v < imin):
                        return (f"{step.tool}.{key} items must be >= {imin}, "
                                f"got {v!r}")
    return None
