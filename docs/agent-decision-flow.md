# Agent decision flow

How a request becomes a report, and what happens at every point where the model
could fail.

```mermaid
flowchart TD
    Q[user request] --> C{report verb or free text?}
    C -->|report kpi/drift/anomalies| R[canned plan<br/>no model]
    C -->|ask| L[Claude + tool schemas<br/>structured output]
    L --> V{every step valid<br/>against the registry?}
    V -->|no, or API failed, or refused| K[keyword router<br/>+ note for the limits section]
    V -->|yes| P[plan, source=llm]
    R --> E[executor]
    K --> E
    P --> E
    E --> T[8 WP2 tools<br/>parameterised SQL over DuckDB]
    T --> RES[ToolResults<br/>values + provenance + status]
    RES --> N{narrate?}
    N -->|ask, model available| LN[Claude writes prose<br/>around fixed numbers]
    N -->|report, or model failed| DN[deterministic summary]
    LN --> RD[renderer]
    DN --> RD
    RES --> RD
    RD --> OUT[report.md + PNGs + trace.json<br/>-> report.html]
```

## The one sentence that makes this safe

**The model plans and narrates; it never computes.**

Every number in every report is produced by parameterised SQL inside a WP2 tool.
The model chooses *which* tools to run and writes prose *around* the results it
is handed. It has no store access and no arithmetic role. The figures, the
tool-call trace and the limits section are all rendered from the `ToolResult`s
regardless of what the model says — which is why a model failure costs
readability and never correctness, and why the fallback at every stage is a
working report rather than an error.

## Why the three `report` verbs bypass the model entirely

`report kpi`, `report drift` and `report anomalies` run fixed plans defined in
`agent/router.py`. No API call is made, no key is needed, and the same store and
period give identical output every time, apart from the generation timestamp.

That matters for three reasons. It is the **reproducible demo path** — a marker
runs `scripts/demo.sh` and gets the reports committed under `docs/reports/`,
identical apart from the generation timestamp in the header. It is the
**offline fallback** — the tool is useful with no credentials and no network. And it is the **reference** the agentic path is
checked against: the same tools, the same SQL, the same numbers, so any
difference between an `ask` report and a `report` report is a difference in
*which analyses were chosen*, never in what they computed.

The agentic behaviour lives entirely in `ask`.

## The three constraints on the planner

`ask` sends the question and the registry's tool schemas to Claude and gets back
a *plan*: an ordered list of tool calls with a rationale for each. Three
independent constraints mean a bad plan degrades instead of breaking.

**1. Structured outputs pin the shape.** The request carries a JSON schema
generated from the registry itself (`registry.plan_json_schema()`), so the reply
is JSON of the right shape or the API rejects it. The schema is derived from the
same `TOOLS` table the executor dispatches on, so it cannot drift out of sync
with what actually exists.

Anthropic's structured outputs require every object to set
`additionalProperties: false` and to list every property in `required`. The tool
arguments are therefore a single flat object containing the union of every
tool's parameters, and the model must emit every key on every step — setting the
ones it is not using to `null`. `plan.effective_args()` drops those nulls, and
validation, execution and figure selection all read a step through it so they
cannot disagree about what the step says.

**2. The registry validates every step.** `registry.validate_step()` rejects a
tool name the model invented, an argument a tool does not take, and an argument
outside its allowed values. A plan is accepted only if every step passes.

**3. Any failure falls back to the keyword router.** Missing SDK, missing key,
network error, rate limit, refusal, unparseable JSON, an invalid step — all of
them produce the same outcome: `router.route()` picks a canned plan by keyword,
and the reason is carried into the report's *Confidence and limits* section.

`planner.plan()` never raises. It returns a `Plan` on every path, and the report
always says which path it took:

> **Planning.** planning failed: the call failed: "Could not resolve
> authentication method…"; the keyword router selected the drift plan (3 keyword
> match(es)).

## Why the executor converts exceptions to values

`executor.execute()` runs each step inside a total error boundary. A tool that
raises, a tool that returns the wrong type, a step that fails validation — each
becomes a `ToolResult` with `status="error"` and a message, and the plan
continues to the next step.

An agent that receives an exception cannot reason about it; an agent that
receives a *value* saying "this analysis could not answer, and here is why" can
route around it and report the gap. A four-step plan where step two fails still
produces a report containing the other three answers and a limits section naming
the failure. That is strictly more useful than a traceback, and it is the reason
a malformed period like `--period February` exits 0 with an explanatory report
rather than crashing.

The catch-all is deliberate and is the last line of defence: the eight tools are
written not to raise, and the boundary exists for the case where one does anyway.

## Narration

For `ask`, the tool results are handed to Claude verbatim — values, status,
message and provenance — and it writes the *Findings* and *Next checks*
sections. Its instructions forbid stating a number that is not in the results it
was given.

That instruction is a quality measure, not a safety measure. The safety comes
from structure: the model's prose occupies two sections of the report, while the
scope figures, every plot, the full tool-call trace and the entire limits section
are rendered from the `ToolResult`s underneath it. A narrator that hallucinated
a success rate would be contradicted by the trace on the same page — and a
narrator that fails at all is replaced by `render.summarise()`, the deterministic
summary the `report` verbs already use.

The report footer always discloses both choices:

    narrative source: template, plan source: router

## What lands on disk

| file | what it is |
|---|---|
| `report.md` | the source of truth; six mandated sections plus the trace |
| `report.html` | self-contained — every PNG inlined as a data URI, no external requests |
| `trace.json` | every tool call, its effective arguments, status and rows scanned |
| `*.png` | figures, drawn from `ToolResult`s only |
| `report.pdf` | best-effort, only when WeasyPrint and its native deps are present |

The trace is both the rubric's "clear tool-use flow" and the first place to look
when a number surprises you: it records the arguments the tool was *actually*
called with, after null-stripping, so a plan and its execution can never be
described differently.
