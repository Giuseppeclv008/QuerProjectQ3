# Committed reports — what is current, and what is not

These four directories are committed artifacts: a reader opens them expecting
the numbers to be the ones this code produces. **They are current.** All four
were regenerated on 2026-08-19 against a freshly rebuilt three-month store
(55,132,433 rows, 36 heads, 2026-01-31 16:00:06 → 2026-04-30 16:59:59), with
every fix that had invalidated them in place. The table below is empty, which
is what "the committed reports match the code" looks like.

| Artifact | Invalidated by | What changes on regeneration |
|---|---|---|

*(empty — nothing outstanding)*

What the regeneration measured, for the record. The idle fix (`6e1b9be`) was
worth more than the note predicted: on February, like for like, the reported
total fell from **11,551.3 head-hours to 7,094.1** — a 39% overstatement, and
the period count *rose* from 22,459 to 24,813, exactly as breaking runs at
holes in the data implies. On the full three months the figure is **7,228.1
head-hours across 25,046 periods**. The head-agreement sentence (`baa4819`) is
now scoped to shape and carries its own caveat about level.

One difference worth naming rather than hiding: `ask-live-sample/` has no
figures this time. The model planned one step (`head_correlation`) where the
previous run planned two, so there was no second series to plot. That is the
model's choice on a fresh run, not a regression — and re-rolling until it
produced a prettier artifact would have made this directory evidence of
nothing. The 7B still falls back to the deterministic narrator, as it did on
2026-07-26 and 2026-08-16; the prose is the template's, rendered from real tool
results.

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
