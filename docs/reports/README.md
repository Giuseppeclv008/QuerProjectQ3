# Committed reports — what is current, and what is not

These four directories are committed artifacts: a reader opens them expecting
the numbers to be the ones this code produces. **They are current.** All four
were regenerated on 2026-10-09 against the same three-month store
(55,132,433 rows, 36 heads, 2026-01-31 16:00:06 → 2026-04-30 16:59:59), by the
code of `fix/agentic_call` after the last change to what a report prints. The
table below is empty, which is what "the committed reports match the code"
looks like. The previous regeneration was on 2026-08-19.

| Artifact | Invalidated by | What changes on regeneration |
|---|---|---|

*(empty — nothing outstanding)*

What the regeneration measured, for the record. The idle fix (`6e1b9be`) was
worth more than the note predicted: on February, like for like, the reported
total fell from **11,551.3 head-hours to 7,228.1** — a 37% overstatement, and
the period count *rose* from 22,459 to **25,046**, exactly as breaking runs at
holes in the data implies. Those are the figures `kpi-2026-02/report.md` now
carries, from `idle_periods(period='2026-02')` over 21,971,506 scanned rows.
This paragraph used to quote a second pair for February and attribute the pair
above to a three-month run; no three-month idle run is committed, and the
February report is the only idle evidence in this directory. The head-agreement
sentence (`baa4819`) is now scoped to shape and carries its own caveat about
level.

What the 2026-10-09 regeneration changed in the three fixed reports (they use no
model, so the numbers are the same and the layout is not): each now carries the
tables of the results that are a row per head or per day, the share of all the
rejects that the weakest head holds with the median head beside it, the heads
and days with the most rejects, and, for idle time, the longest period and the
spread across heads; `anomalies-2026-02/` gains a chart of the rejects per day.
There is no *Answer* line in them, because they have no model: that line exists
only in `ask` reports.

`ask-live-sample/` is a different run, not an update of the old one. The
August sample was `qwen2.5:7b` on a Windows laptop, asked "which head behaves
differently from the others, and why?" over February: one step
(`head_correlation`), and a narration the detector rejected, so its prose was the
template's. The 2026-10-09 sample is `qwen3:14b` under Ollama 0.40.2 on an
Apple M5 Pro (24 GB), asked "Is there a head with an unusual number of failed
closures?" over the whole store, planning tier `plan`: plan source `llm`, two
steps (`success_rates` by head, `torque_stats` of the failed closures by head),
and a narration the model wrote and the checks accepted (*Answer* bullet:
head 29 with 117 rejected, head 35 with 78, head 1 with 69). It was asked once,
after one unrelated warm-up question, and committed as it came; re-rolling until
it produced a prettier artifact would have made this directory evidence of
nothing. Its third finding, the torque variability of the *failed* closures of
head 7, is the model's choice of a second step and not a result anyone should
build on: a head with five rejects has no meaningful standard deviation. The
question is the one chosen for the demo, so the numbers match the slide
(117 of the 1,095 rejects, 3.8 times the per-head mean of 30.4).

## Regenerating

From the repository root, with the raw month zips present:

```bash
scripts/build_store.sh events_3mo.duckdb telemetry_*.zip   # out path is required
scripts/demo.sh                                            # kpi, anomalies, drift
```

`build_store.sh` takes the output path as its first argument and exits on its
own usage line if given none. `demo.sh` defaults to `events_3mo.duckdb` at the
repository root and writes into this directory, replacing each report — which
removes its banner along with the stale numbers.

`ask-live-sample/` additionally needs a reachable model — see
`docs/validation-log.md` for the run that produced the committed copy and the
provider settings it used.

When a directory is regenerated, delete its row here. The generator rewrites
both `report.md` and `report.html` — `demo.sh` for the first three, the `ask`
verb for `ask-live-sample/` — so their banners go with the stale numbers and
need no separate removal. An empty table means the committed reports match the
code.
