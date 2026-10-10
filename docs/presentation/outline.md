# Presentation outline — 12 slides

Numbering follows the deck: the title slide carries no number, so the problem
is slide 1 and the demo slide 11; the deck closes on an unnumbered "Thank you".
In PowerPoint or Canva a slide's page is its number plus one.

Every bullet is written to be transcribed onto a slide as-is. Numbers are
measured on `events_3mo.duckdb` (55,132,433 rows, machine `MCC`, 36 heads,
2026-01-31 16:00:06 → 2026-04-30 16:59:59 — the store fingerprint; the
day-files are offset from midnight) and reconciled in
[`docs/validation-log.md`](../validation-log.md).

---

## Title

**Agentic AI for Telemetry Analysis on AROL Capping Machines**

- System and Device Programming — Politecnico di Torino
- Project proposed by AROL Group (Prof. Quer)
- Presented by Francesco Ambrosino, Giuseppe Antonio Calvello and Stefano Alverino
  (no date on the slide)

---

## 1. The problem

- An AROL Equatorque capping machine polls **36 capping heads at ~1 Hz** and
  uploads one wide CSV per day: ~86,400 rows × 109 columns, ~1.6 GB/month
  unzipped (the month zips are 26–42 MB).
- **89 day-files** in the provided pool.
- The PLC reports **state, not events**. ~24.5% of rows are exact consecutive
  duplicates, and the per-head cap counters only advance when a cap is actually
  applied.
- So a "closure" is not in the data — it has to be **reconstructed from a counter
  delta**. Getting that right is the whole first tier; everything downstream is
  only as good as it.
- The ask: refine the raw pool, then let an agent answer questions about it and
  write a report an engineer can act on.

---

## 2. Architecture

- Four tiers, each independently testable:
  1. **C++ MAS ingestion** — CSV pool → dedup → closure reconstruction → DuckDB
  2. **`cap_events` store** — DuckDB, the single source of truth
  3. **Python analytics toolkit (WP2)** — 13 pure functions, SQL in (all but
     `methodology`), typed result out
  4. **Report agent (WP3) + CLI (WP4)** — the LLM plans and narrates, never computes
- The slide redraws the **C4 container view** from the README
  (`docs/diagrams/C4_Container.png`), each box tagged with its tier.
- *Said aloud, no longer on the slide:* configuration (WP5) cuts across all of it: no path, band or threshold is
  hard-coded.

---

## 3. WP1 — ingestion

- Closure detection by **counter delta per head**, not by any single column.
- Four cases: normal increment, aggregated increment (delta > 1), counter reset,
  stalled counter (no event).
- Consecutive-duplicate elimination before reconstruction; idempotent
  reprocessing, so re-running a day-file cannot double-count.
- Staging + merge write path into DuckDB; cross-worker merge unifies per-worker
  stores.
- Result: **55,132,433 closure events** over three months, 36 heads.
- Validated against an **independent Python oracle** — the C++ output and a
  raw-CSV re-derivation agree exactly.

---

## 4. Performance

- Three architectures benchmarked: single-file `clean`, multi-threaded monolith,
  distributed MAS (coordinator + workers over ZeroMQ).
- *Said aloud, no longer on the slide:* sweep: 1 / 7 / 28-day volumes × all architectures × 3 repeats — **81/81 runs
  oracle-exact**.
- Resilience shown, not claimed: worker SIGKILL mid-run and coordinator death
  with an orphan worker both recover (chaos E2E).
- **Headline finding: the merge is what is left to win.** MAS N=16 runs the
  month end-to-end at **3.83×** the sequential baseline (537.8 s → 140.4 s); the
  clean phase alone parallelizes at **7.2×**. The gap between the two is the
  unification cost, *flat* in N: 64.8 s at N=16 and 65–71 s from N=2 to N=16
  (mono-MT 70–73 s), 46% of MAS N=16's wall clock. It now only moves rows —
  under the old key it grew with store count, and that growth was the defect
  doing work. Amdahl on the serial fraction, not a failure to scale.
- The slide's chart is wall clock per architecture, clean + merge: mono-1T 537.8 s,
  mono-MT T=8 157.3 s, MAS N=16 140.4 s, captioned *serial merge, not the
  workers, caps the speedup*.
- *Said aloud, no longer on the slide:* name the fix (partitioned Parquet or a concurrent-writer store)
  as roadmap, not as done.

---

## 5. The data, measured

- `status` is **a bitmask, not an enumeration** — bit 0 is the reject signal,
  bits 1–6 are the conditions. AROL's brief (its slide 6) lists 13 codes:
  0 (Closure OK), then each condition without and with the reject bit —
  2/3, 4/5, 8/9, 16/17, 32/33, 64/65.
- A closure is a rejection **if and only if** its status is odd.
- Measured over three months. The slide captions the table *MEASURED ·
  FEB–APR 2026 · 55,132,433 CLOSURES / Only 5 of the 13 codes occur* — 0, 2, 4, 9
  and 65; the other eight never appear in the pool:

  | status | torque>0 | count | decoded |
  |---|---|---:|---|
  | 0 | yes | 31,655,161 | clean |
  | 2 | no | 23,447,151 | No Load — the idle cycle |
  | 0 | no | 16,552 | clean status, no torque applied |
  | 2 | yes | 12,461 | No Load with torque |
  | 65 | yes | 1,071 | Bad Closure + reject |
  | 9 | yes | 24 | No InTorque + reject |
  | 4 | — | 12 | No Closure, not rejected |
  | 65 | no | 1 | Bad Closure, no torque |

- 1,071 + 24 + 1 = **1,096 rejects**, exactly what the odd-status rule returns.
  The bitmask is confirmed by the data, not assumed.
- *Said aloud, no longer on the slide:* **this changed a number.** The earlier rule `status == 65` undercounts: February
  has **748** rejected closures.
- *Said aloud, no longer on the slide:* **success rate excludes no-load cycles** — a head that only ever cycled with no
  load performed zero capping operations and is omitted, not reported at 0%.

---

## 6. WP2 — the analytics toolkit

- Thirteen pure functions: `overview`, `success_rates`, `torque_stats`,
  `capping_speed`, `idle_periods`, `anomalies`, `trend`, `head_correlation`,
  `event_gaps`, `compare_periods`, `closure_filter`, `failure_correlation`,
  `methodology`.
- Signature is uniform: `(Config, **kwargs) -> ToolResult`. Parameterised SQL in,
  typed result out. `methodology` is the exception that runs no SQL: it restates
  the documented pipeline rules (preprocessing, duplicates, cleaning assumptions,
  classification), each claim pinned by a test.
- *If asked, not on the slide:* the last five were added on 2026-10-08/09, each
  for a question of the brief no tool answered: machine stops (`event_gaps`),
  months or weeks side by side (`compare_periods`), an exact count against a
  value the operator names (`closure_filter`), the reject rate against the hour
  of day or the torque (`failure_correlation`), how the data was cleaned
  (`methodology`). Every tool also states what it does *not* measure; the
  planner sees it and the report's limits print it.
- **`ToolResult` carries its own provenance**: status
  (`ok`/`insufficient_data`/`error`), values, period, rows scanned, every filter
  applied, every assumption made.
- **Nothing below the CLI raises.** A gap in the data is a `status`, not an
  exception — which is what lets an unattended run still produce a report.
- Provenance is not decoration: it populates the report's *Confidence and limits*
  section, so "100%" and "100% of four closures" cannot look the same.

---

## 7. WP3 — the report agent

- Show the **decision flowchart** ([`docs/agent-decision-flow.md`](../agent-decision-flow.md)).
- **The model plans and narrates; it never computes.** Every number comes from
  the same deterministic SQL either way.
- Three constraints on the planner, so a bad plan degrades instead of breaking:
  1. **Structured outputs** pin the reply to a JSON schema *generated from the
     tool registry itself*, so it cannot drift from what exists.
  2. **Registry validation** rejects an invented tool, an argument a tool does
     not take, or a value outside its allowed set.
  3. **Router fallback** — no model reachable (Ollama down, no key, no
     network), a refusal, bad JSON or an invalid step all fall back to a keyword
     router, and the reason is printed in the report's limits section.
- *In the flowchart, said if asked:* **the system amends one kind of plan.** A
  model plan that trends torque gets `compare_periods` added (by week within a
  month, by month over longer), because the drift test reads a steady trend and
  misses a step in level: in March 2026 the median torque goes 1.999 → 1.748 →
  2.198 Nm while no head drifts. The report says the step was added.
- Two narrator paths. The local model (the default) is handed the deterministic
  findings, the tables and the list of figures — never the raw results — and
  writes one to three sentences under an **Answer** bullet, above the findings;
  an answer that states a number the findings do not carry is rejected and the
  template stands alone. A hosted model gets the results verbatim and writes
  *Findings* and *Next checks*; there the number rule is asked, not checked.
  Either way the figures, the trace and the limits are rendered from the
  `ToolResult`s underneath, so a narrator failure costs readability, never
  correctness.

---

## 8. WP4 — the BOT

- Four commands:

      arol report kpi       --period 2026-02
      arol report drift     --period 2026-02..2026-04
      arol report anomalies --period 2026-02
      arol ask "Is there a head with an unusual number of failed closures?"

- **The three `report` verbs have no model in them.** Same store, same period,
  the same report every time, bar the generation timestamp — that is what makes
  the demo reproducible and gives the offline path.
- Each run writes a self-contained directory: `report.md`, `report.html` (plots
  inlined as data URIs, no external requests), `trace.json`, PNGs.
- Failure policy is deliberate: a **config** problem exits 2 before any work; an
  **analysis** gap produces a report that names the gap, because an unattended
  run must still land on disk.
- **`ask` runs on a local model by default** (the code default on `main` since
  `fix/agentic_call` was merged, 2026-10-09): Ollama with qwen3:14b, no API key,
  nothing leaves the machine.
- *Said aloud, no longer on the slide:* another model: `"model"` in `arol.json`, or `--model` after
  the question. The hosted Anthropic API is wired in (`--provider anthropic`)
  but untested.
- Show a generated report — the six mandated sections and the tool-call trace.

---

## 9. A finding

- The machine-level number looks perfect: **99.9950%** success over February
  (14,817,976 successful, 748 rejected).
- Per head, it is not evenly spread. Over three months **head 29 accounts for 117
  of the 1,095 rejected capping operations** — against a per-head mean of 30.4,
  and against 78 for the next-worst head (35). **3.8× the per-head average.**
- That is the actionable finding, and the headline rate hides it completely.
  In February, 99.9950% (machine) and 99.9781% (head 29) look like the same
  number until you count rejects per head.
- **Checked, not trusted** (box on the slide): every figure on it was recomputed
  with SQL written independently of the toolkit (`docs/validation-log.md`,
  2026-08-22), and a standard-library Python oracle re-derives the closures from
  the raw CSV and matches the C++ on every field.
- **If asked "and head 35?"** — which is the natural question once 78 is on the
  slide. First-order Poisson check on a per-head mean of 30.4 (sigma ~5.5):
  head 29 sits ~15.7 sigma above the machine mean, which is not arguable. Head
  29 against head 35 is 117 vs 78, a difference of 39 against a combined sigma
  of sqrt(117+78) ~ 14, so **~2.8 sigma**. The defensible claim is that 29 and
  35 are *both* outliers against the machine, that 29 is the worse of the two,
  and that the gap between them is real but not overwhelming. Assumes
  independent uniform rates — a reasonable first approximation, not a model.
- *Said aloud, no longer on the slide:* **what we did *not* find.** No head exceeds the Mann-Kendall
  drift threshold on torque or on success rate over three months, and all 36
  heads correlate above 0.9999 on mean torque — none is out of step *in shape*.
  The machine is stable; head 29 is a discrete problem, not a trend.
- *Said aloud, no longer on the slide:* **what that correlation cannot see.** Pearson is invariant to a per-head
  offset, so a head running steadily below the others while moving with them
  scores ~1 and is reported as tracking. The report says so, and names the check
  that would catch it: per-head median torque (`torque_stats by head`).
- *Said aloud, no longer on the slide:* reporting the absence honestly is a feature. An earlier version of the report
  always named a "least-correlated head", which on this data asserted that *the
  odd head out has a correlation of 1.000* — true arithmetic, false conclusion.

---

## 10. Honest limits

- **`NUM_HEADS` is compile-time 36.** The brief's own example shows a 48-head
  machine; no 48-head data exists to test against. Known limit, roadmap item.
- **GPU ingestion: CSV only.** The CPU path reads CSV, Parquet and JSON day-files through
  `open_raw_reader()`: a real day (2026-02-28, 806,785 events) converted to
  Parquet and JSON gives stores identical row for row. `--engine=cuda` parses CSV
  text itself and refuses the other two (exit 2). The store was never CSV-bound:
  DuckDB or Parquet, read through the same `cap_events` view by every tool that
  queries it (`test_backend_parity.py` checks the original eight).
- **~0.02% of closures carry statuses we decode but have not seen AROL confirm** —
  12,461 No-Load-with-torque and 12 No-Closure rows. We treat them as carrying no
  pass/fail verdict and exclude them from the rate rather than guessing.
- **The live agentic path is proven on a local model, not on the hosted one.**
  qwen3:14b, the default, was asked the brief's 43 example queries through
  `arol ask`, and every answer was checked against figures recomputed with SQL
  that shares no code with `analytics`: **41 pass, 2 known weak** (25 and 32,
  marked `xfail`). The check is `python/tests/test_brief_queries_live.py`,
  opt-in (`AROL_LIVE_QUERIES=1`, store and model needed; 8 min 45 s on the run
  of 2026-10-09, `docs/validation-log.md`). `docs/reports/ask-live-sample/` is the
  demo question asked once on qwen3:14b (2026-10-09): two steps, and a narration
  the checks accepted. What stays unverified is **schema
  acceptance against the Anthropic API**, because no key has ever been used;
  `test_anthropic_schema_live.py` sends the schemas and is gated on one.
- **Numbers checked, words not.** With the local model, any number in the answer
  that the findings do not carry rejects the answer, and the deterministic
  summary stands alone. A wrong claim built on right figures passes: asked
  "Which capping head behaves differently from the others?" (query 25,
  February), the model names head 9 while the finding printed beside it says
  *No head stands out* (head 9's sigma is 1.5% above the median of the others).
  The sentence is the model's, the finding the template's, and both are on the
  page.
- *If asked, no longer on the slide:* **why not the 7B.** qwen2.5:7b planned but
  never narrated: every narration was rejected by the no-bullet detector (3 of 3
  in July, 2 of 2 in August), so the prose of the August sample was the
  template's. Handed the raw results, qwen3:14b misread them ("all 36 heads show
  torque drifts" when none did), which is why the local narrator now reads the
  verified findings instead.
- *If asked, no longer on the slide:* **PDF export needs native dependencies** (WeasyPrint + Cairo/Pango). Markdown
  and HTML always ship; `--pdf` degrades with an install hint.
- **The merge is unfixed**, and it is the serial fraction that holds end-to-end
  speedup at 3.83× while the clean phase alone reaches 7.2×.

---

## 11. Demo

- One command reproduces everything:

      scripts/demo.sh

  55.1 M rows, three report types, 12/12 tool steps `ok`, ~5 s (5.2 s on
  2026-10-09, warm).
- Live on qwen3:14b via local Ollama, from `main`: `arol ask "Is there a head
  with an unusual number of failed closures?"`, with no `--period`, so the whole
  store and the figures of slide 9 (head 29 with 117 of the 1,095 rejects, head
  35 with 78). Show the plan the model chose. It is the brief's query 26: the
  live test passes it on February, and over the whole store the evidence is
  the committed run below. Then the same question with `--provider anthropic` and
  no key: it falls back to the router (the anomalies plan) and *says so* in the
  report. The flag is not optional: the code defaults to Ollama, so unsetting
  the key alone changes nothing and the model simply plans again.
- Committed artifacts: [`docs/reports/`](../reports/) — `kpi-2026-02`,
  `drift-2026-02_2026-04`, `anomalies-2026-02`, and `ask-live-sample` (the demo
  question on qwen3:14b, 19 s warm), all regenerated on 2026-10-09.
- Close on the invariant: **the model chose the analyses; the SQL produced every
  number.**

---

## Provenance of the numbers

Every figure in this outline is derived from `events_3mo.duckdb` as rebuilt on
2026-08-11 under the `(machine_id, head_id, ts)` identity: **55,132,433 rows**,
against 20,347,822 before. The old `(machine_id, head_id, cap_seq)` key was
collapsing distinct closures across the PLC's counter reset — see
`docs/validation-log.md` for the measurement that settles it.

Re-derived from the rebuilt store, not carried over:

- February success rate and counts, and the per-head rate for head 29
- the three-month status distribution in section 5, and the 1,096 reject total
- head 29's share of the rejects

**The finding survived the rebuild but got smaller, and the smaller number is
the one to present.** On the old residue head 29 looked like 75 of 600 rejects
against a next-worst of 37 — 4.5x the machine mean. On the full data it is 117
of 1,095 against a next-worst of 78, i.e. **3.8x**. Still the clear outlier,
still the actionable finding, but the gap to the second-worst head is half what
it appeared to be.

To regenerate everything from scratch:

    scripts/build_store.sh events_3mo.duckdb telemetry_*.zip
    scripts/demo.sh

The store is ~2.6 GB and a single pass needs ~5 GB free. Build it month by month
if disk is tight — the store appends and the ts key makes loading
order-independent.
