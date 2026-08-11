# Presentation outline — 13 slides

Every bullet is written to be transcribed onto a slide as-is. Numbers are
measured on `events_3mo.duckdb` (55,132,433 rows, machine `MCC`, 36 heads,
2026-02-01 → 2026-04-30) and reconciled in
[`docs/validation-log.md`](../validation-log.md).

---

## 1. Title

**Agentic AI for Telemetry Analysis on AROL Capping Machines**

- System and Device Programming — Politecnico di Torino
- Project proposed by AROL Group (Prof. Quer)
- Team members and date

---

## 2. The problem

- An AROL Equatorque capping machine polls **36 capping heads at ~1 Hz** and
  uploads one wide CSV per day: ~86,400 rows × 109 columns, ~1.6 GB/month zipped.
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

## 3. Architecture

- Four tiers, each independently testable:
  1. **C++ MAS ingestion** — CSV pool → dedup → closure reconstruction → DuckDB
  2. **`cap_events` store** — DuckDB, the single source of truth
  3. **Python analytics toolkit (WP2)** — 8 pure functions, SQL in, typed result out
  4. **Report agent (WP3) + CLI (WP4)** — Claude plans and narrates, never computes
- Show the **C4 container diagram** from the README.
- Configuration (WP5) cuts across all of it: no path, band or threshold is
  hard-coded.

---

## 4. WP1 — ingestion

- Closure detection by **counter delta per head**, not by any single column.
- Consecutive-duplicate elimination before reconstruction; idempotent
  reprocessing, so re-running a day-file cannot double-count.
- Staging + merge write path into DuckDB; cross-worker merge unifies per-worker
  stores.
- Result: **55,132,433 cap events** over three months, 36 heads.
- Validated against an **independent Python oracle** — the C++ output and a
  raw-CSV re-derivation agree exactly.

---

## 5. Performance

- Three architectures benchmarked: single-file `clean`, multi-threaded monolith,
  distributed MAS (coordinator + workers over ZeroMQ).
- Sweep: 1 / 7 / 28-day volumes × all architectures × 3 repeats — **81/81 runs
  oracle-exact**.
- Resilience shown, not claimed: worker SIGKILL mid-run and coordinator death
  with an orphan worker both recover (chaos E2E).
- **Headline finding: the merge is the bottleneck.** A 35–54 s unification cost
  caps end-to-end speedup at ~1.15× no matter how many workers are added.
- Show the speedup chart. Name the fix (partitioned Parquet or a
  concurrent-writer store) as roadmap, not as done.

---

## 6. The data, measured

- `status` is **a bitmask, not an enumeration** — bit 0 is the reject signal,
  bits 1–6 are the conditions. Slide 6 of the brief lists 14 rows = 7 conditions
  × {reject, no reject}.
- A closure is a rejection **iff its status is odd**.
- Measured over three months:

  | status | torque>0 | count | decoded |
  |---|---|---:|---|
  | 0 | yes | 11,902,090 | clean |
  | 2 | no | 8,433,525 | No Load — the idle cycle |
  | 2 | yes | 5,452 | No Load with torque |
  | 65 | yes | 585 | Bad Closure + reject |
  | 9 | yes | 15 | No InTorque + reject |
  | 4 | — | 7 | No Closure, not rejected |

- 585 + 15 = **600 rejects**, exactly what the odd-status rule returns. The
  bitmask is confirmed by the data, not assumed.
- **This changed a number.** The earlier rule `status == 65` undercounts: February
  has **748** rejected closures.
- **Success rate excludes no-load cycles** — a head that only ever cycled with no
  load performed zero capping operations and is omitted, not reported at 0%.

---

## 7. WP2 — the analytics toolkit

- Eight pure functions: `overview`, `success_rates`, `torque_stats`,
  `capping_speed`, `idle_periods`, `anomalies`, `trend`, `head_correlation`.
- Signature is uniform: `(Config, **kwargs) -> ToolResult`. Parameterised SQL in,
  typed result out.
- **`ToolResult` carries its own provenance**: status
  (`ok`/`insufficient_data`/`error`), values, period, rows scanned, every filter
  applied, every assumption made.
- **Nothing below the CLI raises.** A gap in the data is a `status`, not an
  exception — which is what lets an unattended run still produce a report.
- Provenance is not decoration: it populates the report's *Confidence and limits*
  section, so "100%" and "100% of four closures" cannot look the same.

---

## 8. WP3 — the report agent

- Show the **decision flowchart** ([`docs/agent-decision-flow.md`](../agent-decision-flow.md)).
- **The model plans and narrates; it never computes.** Every number comes from
  the same deterministic SQL either way.
- Three constraints on the planner, so a bad plan degrades instead of breaking:
  1. **Structured outputs** pin the reply to a JSON schema *generated from the
     tool registry itself*, so it cannot drift from what exists.
  2. **Registry validation** rejects an invented tool, an argument a tool does
     not take, or a value outside its allowed set.
  3. **Router fallback** — no key, no network, a refusal, bad JSON or an invalid
     step all fall back to a keyword router, and the reason is printed in the
     report's limits section.
- The narrator gets the results verbatim and writes two sections. The figures,
  the trace and the limits are rendered from the `ToolResult`s underneath it, so
  a narrator failure costs readability, never correctness.

---

## 9. WP4 — the BOT

- Four commands:

      arol report kpi       --period 2026-02
      arol report drift     --period 2026-02..2026-04
      arol report anomalies --period 2026-02
      arol ask "which head behaves differently, and why?"

- **The three `report` verbs have no model in them.** Same store, same period,
  the same report every time, bar the generation timestamp — that is what makes
  the demo reproducible and gives the offline path.
- Each run writes a self-contained directory: `report.md`, `report.html` (plots
  inlined as data URIs, no external requests), `trace.json`, PNGs.
- Failure policy is deliberate: a **config** problem exits 2 before any work; an
  **analysis** gap produces a report that names the gap, because an unattended
  run must still land on disk.
- Show a generated report — the six mandated sections and the tool-call trace.

---

## 10. A finding

- The machine-level number looks perfect: **99.9943%** success over February
  (14,817,976 successful, 748 rejected).
- Per head, it is not evenly spread. Over three months **head 29 accounts for 75
  of the 600 rejected closures** — against a per-head mean of 16.7, and against
  37 for the next-worst head. **4.5× the machine average.**
- That is the actionable finding, and the headline rate hides it completely.
  99.9943% and 99.9660% look like the same number until you count rejects per
  head.
- **Equally important: what we did *not* find.** No head exceeds the Mann-Kendall
  drift threshold on torque or on success rate over three months, and all 36
  heads correlate above 0.9999 on mean torque. The machine is stable; head 29 is
  a discrete problem, not a trend.
- Reporting the absence honestly is a feature. An earlier version of the report
  always named a "least-correlated head", which on this data asserted that *the
  odd head out has a correlation of 1.000* — true arithmetic, false conclusion.

---

## 11. Engineering

- **201 Python tests, 73 C++ tests**, all green; test output pristine.
- **Golden-report regression**: a fixed store and a fixed plan must render
  byte-identical Markdown, so a change in any tool's SQL shows up as a diff in a
  committed file instead of a silent shift in a number nobody re-read.
- **Orchestration tested with a mocked model** — no tokens, no network. Every
  planner and narrator failure path (no key, API error, refusal, malformed JSON,
  invalid step) is pinned.
- **Independent oracle cross-check**: toolkit SQL against a raw-CSV
  re-derivation.
- **Every number in the three committed reports was reconciled by hand** against
  a direct DuckDB query written independently of the toolkit. That reconciliation
  is what found three reporting defects — none reproducible on the test fixture.
- Config-driven throughout (WP5): no hard-coded path, band or threshold.

---

## 12. Honest limits

- **`NUM_HEADS` is compile-time 36.** The brief's own example shows a 48-head
  machine; no 48-head data exists to test against. Known limit, roadmap item.
- **CSV ingestion only.** The store already exports Parquet; reading JSON or
  Parquet input is a reader sibling, not agent work.
- **~0.03% of closures carry statuses we decode but have not seen AROL confirm** —
  5,452 No-Load-with-torque and 7 No-Closure rows. We treat them as carrying no
  pass/fail verdict and exclude them from the rate rather than guessing.
- **The live-API `ask` path is not covered by the committed evidence.** Every
  planner and narrator test injects a fake client by design, and no API key was
  available in the validation environment. The router-fallback path is verified.
- **PDF export needs native dependencies** (WeasyPrint + Cairo/Pango). Markdown
  and HTML always ship; `--pdf` degrades with an install hint.
- **The merge bottleneck is unfixed**, and it is what caps distributed speedup.

---

## 13. Demo

- One command reproduces everything:

      scripts/demo.sh

  20.3 M rows, three report types, 12/12 tool steps `ok`, ~8 s.
- Live `arol ask "..."` — show the plan the model chose, then show the same
  command with the key unset falling back to the router and *saying so* in the
  report.
- Committed artifacts: [`docs/reports/`](../reports/) — `kpi-2026-02`,
  `drift-2026-02_2026-04`, `anomalies-2026-02`.
- Close on the invariant: **the model chose the analyses; the SQL produced every
  number.**

---

## Numbers still to re-derive before the talk

The store was rebuilt on 2026-08-11 under the `(machine_id, head_id, ts)`
identity: 55,132,433 rows against 20,347,822, because the old `cap_seq` key was
collapsing distinct closures across the PLC's counter reset (see
`docs/validation-log.md`). February's figures above have been re-derived from
the regenerated reports in `docs/reports/`.

**These have not, and the values still in the text are computed on the old
residue. Do not present them as they stand:**

- The status-code table around slide 6 (`585` status-65 rejects, `600` total
  rejects over three months).
- "Head 29 accounts for N of the rejected closures" — the headline finding.
  The direction survives: the regenerated February report still names head 29
  as weakest, at 99.9781% over 411,776 capping operations. The count does not.

Re-derive with the store present:

    scripts/build_store.sh events_3mo.duckdb telemetry_*.zip
    scripts/arol report kpi --period 2026-02..2026-04 --out /tmp/pres

The store is ~2.6 GB and needs ~5 GB free to build in one pass; it was deleted
after the benchmark sweep to make room. Build it month by month if disk is
tight — the store appends and the key makes loading order-independent.
