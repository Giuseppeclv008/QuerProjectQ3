# Agentic Analytics & Reporting — Design Spec

**Date:** 2026-07-11
**Status:** approved (brainstorming), pending implementation
**Supersedes:** parts of `2026-07-04-iiot-data-refinement-mas-design.md` (see §2)
**Implements:** WP2, WP3, WP4, WP5 of the AROL project brief
  (`AROL-presentation-project-Q3.pdf`, "Agentic AI for Telemetry Analysis on AROL Capping Machines")

---

## 1. Why this spec exists

The master spec (2026-07-04) was written against a framing the brief does not support. It
designed a *distributed-MAS scalability project*: C++ agents, ZeroMQ fabric, OpenMP, a
monolith baseline, and a benchmark proving the MAS scales. Plans 1–5 executed that framing
to a high standard and it is all on `main`.

The brief asks for something else. Its title is **"Agentic AI for Telemetry Analysis"**, and
its objective is:

> Design and implement an **Agentic AI application** [that] provides a **BOT interface**
> (CLI, web, or lightweight UI) that can generate analysis reports on demand [...]
> **selecting appropriate analysis steps autonomously**.

Against the brief's five work packages, the project currently stands:

| WP | Requirement | State |
|---|---|---|
| WP1 | Data ingestion & normalization | **done** — except capping speed and validation checks |
| WP2 | Baseline analytics layer (deterministic) | **not built** |
| WP3 | Agentic AI orchestration ("report agent") | **not built** |
| WP4 | BOT interface (CLI preferred) | **not built** |
| WP5 | Engineering quality & reproducibility | partial — no config-driven datasets |

Three of the five evaluation criteria — *quality of analytics outputs*, *agentic
orchestration quality*, *demo clarity* — score work that does not yet exist. This spec
defines that work.

The existing C++ MAS is **not** discarded. It remains the ingestion and cleaning tier, and
it stands on its own for the *software engineering quality* criterion.

## 2. What this spec supersedes

The following, from master spec §5.2 and §5.4, are **out of scope and will not be built**:

- the **C++ KPI Agent** as a ZeroMQ service
- the **Anomaly Agent** as a ZeroMQ service
- the Coordinator's **`analyze` PUSH dispatch** path
- the separate **Results Store**
- the **Isolation Forest / sklearn** model and its train-on-normal-months evaluation

Rationale: the brief never asks for analytics to be distributed. It asks for *deterministic
analysis functions an AI agent can reliably call*. Routing a SQL aggregate through a ZeroMQ
hop adds a failure mode and buys nothing that is graded. WP2 is explicitly required to be
deterministic — "so the AI agent has reliable tools to call" — which rules the ML model off
the critical path. An Isolation Forest may be added later as an optional eighth tool; it is
not planned.

## 3. Data semantics (measured, and a correction to shipped code)

### 3.1 The status codes are documented backwards in our core

`core/include/mas/domain/CapEvent.hpp` states `0 = idle/held, 2 = OK cap, 65 = fault`. The
joint distribution of `(status, torque)` at closure, measured over 2026-02-01 (765,711
closures), says otherwise:

| status | torque | count/day | meaning |
|---|---|---|---|
| `0.0` | > 0 (mean 1.998 Nm, range 1.885–2.317) | 427,643 | **real capping operation, with load** |
| `2.0` | == 0 | 337,772 | **No-Load cycle** — counter advances, no cap applied |
| `65.0` | > 0 | 4 | **fault** |
| `0.0` | == 0 | 137 | transition artifact |
| `2.0` | > 0 | 155 | transition artifact |

`0` cannot mean "idle/held" while carrying ~2.0 Nm on 427k closures; `2` cannot mean "OK cap"
while carrying zero torque on 337k. The `is_fault_status(status) == 65.0` predicate is
correct and unchanged. This reading also reconciles with the brief's own example table, where
status `0` is the normal closure (torque 2.54) and the fault code sits on the low-torque row.

**The store schema does not change.** `status` and `app_torque` are already persisted
faithfully, so no data was lost. Only the interpretation was wrong.

### 3.2 Definitions (locked)

- **Capping operation** := a closure with load, i.e. `torque > 0`. No-Load cycles are
  **excluded from the denominator** of every success metric and reported separately as
  idle/no-load activity.
- **Successful cap** := `status == 0 AND torque > 0`
- **Failed cap** := `status == 65`
- **Success rate** := successful / (successful + failed)
- **No-Load cycle** := `status == 2 AND torque == 0`
- **Idle period** := a sustained run of No-Load cycles for a head, longer than a configured
  minimum duration

These predicates are **config-driven** (§7), so if AROL or the course confirms a different
encoding, it is a config change, not a code change.

### 3.3 The honest headline: the machine is healthy

Success rate against real closures is **~99.999%** (4 faults against 427,643 caps/day). The
brief's illustrative "92.4%" does not hold on this machine. Two consequences:

1. A fault-classification anomaly detector is near-degenerate here. Failure *counting* is not
   where the value is.
2. The signal that matters is **torque drift, per-head variability, and no-load/idle share** —
   all of which the brief explicitly asks for, and all of which are rich in this data.

The system must report this truthfully rather than manufacture alarm. "Failures are
negligible; watch drift" is a finding, not a failure to find anything.

### 3.4 Data available

Three months, 89 day-files: `2026-02` (28), `2026-03` (31), `2026-04` (30). This makes
multi-month drift and trend analysis possible — the original spec assumed a single month.

**Known data hazard:** the Count counter resets mid-month (observed 2026-02, day 16). Any
multi-day aggregation must dedupe on `UNIQUE(machine_id, head_id, cap_seq)`, as the store
already does, and analytics must not assume `cap_seq` is globally monotonic across a month.

### 3.5 Head count is not fixed at 36

Our machine has **36** heads. The brief's example dataset has **48** (`H48 AppTorque`,
`H48 Status`), and states that solutions "must be designed to work on datasets that **may
include, for instance**" that layout. WP5 requires configuration-driven datasets.

**Constraint on this spec's scope:** the analytics toolkit must **derive the head count from
the data/store** and must never hard-code 36. Every tool takes heads as discovered, not as
assumed.

**Known limitation, flagged not fixed:** the C++ core hard-codes
`inline constexpr int NUM_HEADS = 36` sizing `std::array` in `CapEvent.hpp`, so the *cleaning*
tier would need a refactor (`std::array` → `std::vector`, or templating) to ingest a 48-head
dataset. That refactor is **out of scope here** — no 48-head data exists to test against — but
it is a real portability gap and belongs in the roadmap. The analytics tier, which this spec
does define, will not inherit the assumption.

## 4. Architecture

Four tiers. Only the first exists today.

```
┌─────────────────────────────────────────────────────────────┐
│  Ingestion & Cleaning  [C++ MAS — SHIPPED, Plans 1–5]        │
│  raw CSV day-files → dedup transform → cap_events (DuckDB)   │
└────────────────────────────┬────────────────────────────────┘
                             │  cleaned store (read-only)
┌────────────────────────────▼────────────────────────────────┐
│  WP2  Analytics Toolkit  [Python, deterministic]             │
│  pure functions → parameterised SQL → typed results          │
│  every result carries provenance (period, rows, filters)     │
└────────────────────────────┬────────────────────────────────┘
                             │  typed tool contract (one schema)
              ┌──────────────┴───────────────┐
              │                              │
┌─────────────▼──────────────┐  ┌────────────▼─────────────────┐
│  WP3  Report Agent          │  │  WP4  BOT / CLI              │
│  [Python + Claude API]      │  │  report kpi | drift |        │
│  plans tool calls,          │  │         anomalies  (no LLM)  │
│  narrates results,          │  │  ask "<free text>"  (LLM)    │
│  NEVER computes a number    │  │                              │
└─────────────┬───────────────┘  └────────────┬─────────────────┘
              └──────────────┬───────────────┘
                  ┌──────────▼───────────┐
                  │  Reports + Plots      │
                  │  Markdown → HTML/PDF  │
                  └───────────────────────┘
```

**The tool contract is the load-bearing interface.** The same typed signatures that the CLI
calls are what generate the LLM's tool schema. WP2 and WP3 cannot drift apart, and the agent
physically cannot call a tool that does not exist.

## 5. WP2 — Analytics toolkit

Every tool: parameterised SQL in, typed result out, provenance attached. Seven tools, each
justified by queries the brief actually asks (slides 13–18).

| Tool | Answers (from the brief) | Brief requirement |
|---|---|---|
| `overview(period)` | "how many capping operations?", "time range covered?", "any missing/invalid torque?" | exploration + validation |
| `success_rates(period, by=head\|day)` | "success rate per head?", "which head is lowest?", "daily breakdown?" | quality / success-rate |
| `torque_stats(period, filter)` | "average torque for successful closures?", "distribution?", "highest variability?" | torque analytics |
| `trend(signal, window)` | "did average torque change over the month?", "how did success evolve?" | moving averages, drift |
| `anomalies(period, method)` | "torque outside expected range?", "abnormal failure intervals?" | thresholds + statistical deviation |
| `head_correlation(heads)` | "compare head 1 vs head 5", "which head behaves differently?" | correlation checks |
| `idle_periods(min_duration)` | "machine idle state" | sustained No-Load detection |

### 5.1 Result type

Each tool returns a typed object carrying:

- **values** — the numbers/table
- **provenance** — period analysed, rows scanned, filters applied, assumptions in force
- **status** — `ok` | `insufficient_data` | `error` (see §8)

Provenance is not decoration. It is what lets the report populate *confidence/limits*
honestly, and it makes every claim auditable back to a row count.

### 5.2 Drift detection

With three months available, `trend()` computes rolling mean/σ of torque per head plus a
changepoint/trend test — **CUSUM or Mann-Kendall**: deterministic, explainable, no sklearn.
Heads whose torque is *walking* are flagged and ranked by drift magnitude.

On a machine with 4 faults/day, drift is the finding that matters for the predictive/preventive
maintenance the brief names as motivation.

### 5.3 Anomaly detection is statistical, not ML

Two methods, both deterministic:

- **threshold** — torque outside a configured operating band
- **deviation** — per-head robust band (median ± k·MAD, so a few outliers do not inflate it)

The brief calls for exactly this, and requires WP2 to be deterministic.

### 5.4 WP1 gaps to close

Two brief requirements never built, both belonging in the cleaning core:

- **capping speed (pieces/hour)** via incremental average — the inter-cap timing already
  exists in the extractor
- **validation checks** — missing values, timestamp consistency

## 6. WP3 — Report agent

A deliberately boring loop. Boring is the point: it is what makes hallucinated statistics
structurally impossible.

1. **Interpret** — user's question + tool schemas → Claude. Returns a *plan*: an ordered list
   of tool calls with arguments, plus a one-line rationale per call.
2. **Execute** — we run the tools ourselves, in-process. Failures come back as tool *errors*,
   not exceptions; the agent may retry with different arguments or proceed and report the gap.
3. **Synthesise** — results → model, which writes narrative **around numbers it cannot alter**.
4. **Emit** — Markdown (+ HTML/PDF), plots inline, and a machine-readable **trace of every
   tool call and its arguments** appended.

**The LLM plans and narrates. It never computes.** Every number in every report originates in
deterministic SQL.

### 6.1 Report structure (mandated by the brief)

Every report, regardless of type:

> **goal → data used → analyses executed → findings → confidence/limits → next checks**

The tool-call trace is the "clear tool-use flow" the rubric scores, and doubles as the
debugging surface.

## 7. WP4 — BOT interface (CLI) and configuration

```
arol report kpi        --period 2026-02          # fixed plan, no LLM
arol report drift      --period 2026-02..2026-04 # fixed plan, no LLM
arol report anomalies  --period 2026-02          # fixed plan, no LLM
arol ask "which head behaves differently, and why?"   # LLM plans the tool calls
```

The three `report` verbs are the brief's own examples (`report anomalies`, `report drift`,
`report kpi`) and run **deterministic tool plans with no model in the loop**. This is what
makes the demo reproducible and provides the offline fallback. `ask` is where agentic
behaviour lives.

**Configuration (WP5).** Dataset pools, torque bands, drift thresholds, idle minimum
duration, and the success-definition predicates all live in a config file. **No hard-coded
paths** — an explicit evaluation criterion. It also means the status-semantics assumption
(§3.2) is a one-line config change if AROL confirms a different encoding.

## 8. Error handling

The rubric scores **graceful failures**, so failure paths are designed, not bolted on.

- **Tool errors are values, not exceptions.** An empty period, a missing column, or a
  degenerate window returns a typed error the agent reads and routes around. The report names
  the gap in *confidence/limits* rather than dying — or, worse, silently omitting it.
- **The LLM is never load-bearing for correctness.** No API key, no network, rate limit,
  malformed plan, or a hallucinated tool name → fall back to the **keyword router**, which
  picks the nearest canned plan **and says so in the report's limits section**. The numbers
  are identical either way, because the tools produce them.
- **Degenerate data is a first-class case.** A period with no closures, a head that never
  fires, or a month containing a counter reset must produce an honest `insufficient_data`
  finding — never a divide-by-zero, never a fabricated 0%.
- **Config validation up front** — bad paths, unparseable periods, and torque bands that
  exclude all data fail loudly at startup with a usable message.

## 9. Reports and plots (deliverables)

Three templates, all sharing the §6.1 structure. Reports are **committed deliverables**, not
by-products.

- **KPI report** — throughput, success rate overall and per head, no-load/idle share, capping
  speed. Plots: success rate per head (bar), capping speed over time (line).
- **Drift report** — per-head torque trend across three months, changepoint flags, variability
  ranking. Plots: rolling mean ±σ per head (small multiples), drift magnitude ranking.
- **Anomaly report** — threshold and deviation hits, fault events in context, abnormal
  intervals. Plots: torque histogram with band overlay, flagged events over time.

Markdown is the source of truth; HTML and PDF are exports. Plots are matplotlib PNGs written
alongside and referenced from the Markdown, so a report is a **self-contained directory**.

This satisfies the brief's demo requirement: one end-to-end run producing **at least two
different report types**.

## 10. Testing

Mirroring the discipline that worked for the C++ core (and that caught the counter-reset bug).

- **Unit tests per tool** — small deterministic fixtures, hand-computed expected values. Every
  tool's SQL is pinned.
- **Python oracle cross-check** for the headline KPIs, independent of the toolkit's own SQL,
  in the spirit of `python/oracle.py`.
- **Agent tests with a mocked LLM** — assert a given query yields the expected *tool plan*, and
  that a malformed or hallucinated plan degrades to the router instead of crashing. Tests
  orchestration with no tokens and no network.
- **Golden-file tests on reports** — fixed dataset + fixed plan must render a byte-stable
  report, so analytics regressions surface as diffs.
- **One end-to-end test** on a real day-file: ingest → clean → report.

Degenerate cases get explicit tests: empty period, single head, all-no-load, counter reset.

## 11. Implementation plans

This spec is implemented by two plans:

- **Plan 6 — Analytics Foundation.** Status-semantics correction in the C++ core (§3.1),
  WP1 gaps (capping speed, validation checks — §5.4), and the full WP2 toolkit (§5) with tests.
  Independently demoable via direct tool calls.
- **Plan 7 — Agent & BOT.** WP3 report agent (§6), WP4 CLI and config (§7), report templates,
  plots, sample reports, and the end-to-end demo (§9).

Plan 6's typed tool contract is exactly what Plan 7's agent calls, which forces the interface
to be clean before the agent depends on it.

## 12. Open questions

1. **Status encoding confirmation.** §3.2 is inferred from the joint `(status, torque)`
   distribution and is consistent with the brief's example table, but it is an *inference*
   about machine/firmware semantics. Worth confirming with the course/AROL. Parked, not
   blocking: the definition is config-driven, and the brief's own "meta queries" ask what
   assumptions were made during cleaning — so stating this assumption explicitly is itself a
   deliverable.
2. **Reprocessing.** Capping speed (§5.4) is computed in the extractor, so the three months
   must be reprocessed to populate it. Cheap (Plan 5 measured ~87 s/month single-threaded),
   but it is a step.
3. **48-head portability (§3.5).** The C++ cleaning tier cannot ingest the brief's example
   48-head layout without a `NUM_HEADS` refactor. Deferred — no such data exists to test
   against — but it is a genuine gap against "designed to work on datasets that may include"
   that layout. Roadmap item, not a Plan 6/7 task.
