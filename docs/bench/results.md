# Benchmark results (median of 3 repeats)

| arch | n_workers | threads | files | clean_s | merge_s | total_s | events_per_s | peak_rss_mb | cpu_pct |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| mas | 1 | 1 | 1 | 3.515 | 2.2 | 5.715 | 133983 | 222.3 | 177.9 |
| mas | 1 | 1 | 7 | 18.528 | 11.146 | 29.677 | 131449 | 665.6 | 176.8 |
| mas | 1 | 1 | 28 | 98.144 | 70.554 | 168.613 | 129721 | 2818.9 | 185.1 |
| mas | 2 | 1 | 1 | 3.474 | 0.912 | 4.386 | 174581 | 289.7 | 140.8 |
| mas | 2 | 1 | 7 | 12.206 | 4.216 | 16.422 | 237548 | 1055.5 | 277.5 |
| mas | 2 | 1 | 28 | 72.263 | 29.281 | 100.642 | 217331 | 3662.4 | 289.8 |
| mas | 4 | 1 | 1 | 3.531 | 0.964 | 4.495 | 170347 | 340.2 | 150.9 |
| mas | 4 | 1 | 7 | 10.808 | 4.435 | 15.243 | 255922 | 1194.7 | 419.9 |
| mas | 4 | 1 | 28 | 43.699 | 26.87 | 69.389 | 315218 | 4384.4 | 520.5 |
| mas | 8 | 1 | 1 | 3.581 | 0.984 | 4.579 | 167222 | 445.3 | 152.7 |
| mas | 8 | 1 | 7 | 8.251 | 4.437 | 12.695 | 307288 | 1424.3 | 611.8 |
| mas | 8 | 1 | 28 | 32.635 | 25.456 | 58.091 | 376524 | 4849.2 | 708 |
| mas | 16 | 1 | 1 | 3.614 | 0.953 | 4.567 | 167662 | 652.7 | 158.6 |
| mas | 16 | 1 | 7 | 8.357 | 4.168 | 12.652 | 308332 | 1632.7 | 606.2 |
| mas | 16 | 1 | 28 | 29.275 | 25.367 | 55.394 | 394856 | 5688.4 | 814.1 |
| mono-1T | 0 | 1 | 1 | 3.573 | 0 | 3.573 | 214305 | 80 | 103.6 |
| mono-1T | 0 | 1 | 7 | 18.469 | 0 | 18.469 | 211220 | 108.7 | 111 |
| mono-1T | 0 | 1 | 28 | 101.814 | 0 | 101.814 | 214830 | 224.4 | 113 |
| mono-MT | 0 | 2 | 1 | 3.584 | 0.88 | 4.468 | 171377 | 212.7 | 113.2 |
| mono-MT | 0 | 2 | 7 | 10.398 | 4.576 | 14.967 | 260641 | 829.8 | 198 |
| mono-MT | 0 | 2 | 28 | 71.465 | 31.391 | 102.856 | 212653 | 3419.9 | 213.7 |
| mono-MT | 0 | 4 | 1 | 3.451 | 0.929 | 4.365 | 175421 | 195.4 | 112.8 |
| mono-MT | 0 | 4 | 7 | 7.508 | 4.398 | 11.842 | 329422 | 844.3 | 328.1 |
| mono-MT | 0 | 4 | 28 | 54.065 | 32.561 | 85.895 | 254644 | 3605.4 | 367.5 |
| mono-MT | 0 | 8 | 1 | 3.448 | 1.017 | 4.463 | 171569 | 231.8 | 115 |
| mono-MT | 0 | 8 | 7 | 7.204 | 4.387 | 11.591 | 336556 | 894.4 | 405.7 |
| mono-MT | 0 | 8 | 28 | 43.315 | 32.447 | 76.263 | 286806 | 3789.5 | 452.3 |

Caveats: laptop thermals (no fan control), median-of-3, N=16 on 8 cores is a deliberate oversubscription point, merge phase reported separately; mono-MT uses a std::thread atomic-counter pool (dynamic load balancing, slightly fairer than PUSH/PULL round-robin).

## Data notes (real month, re-measured 2026-08-11 under the ts event identity)

- **The store used to discard real capping events, and this benchmark used to
  reward it.** The previous version of this file recorded that "days 16-24
  replay cap_seq ranges already emitted in days 1-15" and that
  `UNIQUE(machine_id, head_id, cap_seq)` "dedupes replayed sequences", so
  21,872,663 processed events "persist as 14,372,237 distinct rows". The replay
  hypothesis was never tested and is false: of head 1's 23,851 day-17 closures
  whose `cap_seq` collides with days 1-15, **18,721 carry a different torque**.
  They are distinct physical caps. The PLC's Count register resets and climbs
  again through values it has already used. Identity is now
  `(machine_id, head_id, ts)` — one head closes at most once per poll — and all
  28 day-files persist in full: **21,872,663 events, 21,872,663 rows**.
- **Every number in the table is therefore larger than the version it replaces,
  and that is the correction, not a regression.** The old run wrote 14.4M rows
  where this one writes 21.9M for the same input, so it was timing 66% of the
  work. 28-day medians, old -> new: mono-1T 87.5 -> 101.0 s, MAS N=16
  75.7 -> 91.2 s, mono-MT T=8 87.3 -> 99.4 s.
- **The correctness oracle now checks something.** `oracle_union.py` counted
  distinct `(head_id, cap_seq)` — precisely the quantity the defect leaves
  stable — so all 81 runs reported "oracle-exact" while a third of February was
  being dropped. It counts distinct `(head_id, ts)` now, and the 28-day
  expectation moved from 14,372,237 to 21,872,663. All 81 runs match it.
- **The merge cost stopped growing with N, and that is diagnostic.** Old
  merge_s at 28 days climbed with store count (N=1 35.1 s -> N=16 50.9 s);
  it is now flat (N=1 65.2, N=2 63.0, N=4 63.3, N=8 62.8, N=16 64.0 s). The
  old growth *was the defect doing work*: more stores meant more colliding
  `cap_seq` for `INSERT OR IGNORE` to resolve, and every resolution threw away
  a real closure. With disjoint sources the merge only moves rows, so its cost
  follows total volume and not how the volume is partitioned.
- **The scaling conclusion survives, slightly weaker.** Clean parallelises
  better than before (mono-1T 101.0 s -> MAS N=16 27.1 s, **3.73x**, up from
  3.50x, because there is more write work to spread). End-to-end MAS still tops
  out over mono-1T but at **1.11x** (91.2 s vs 101.0 s) rather than 1.16x.
  mono-MT still never meaningfully beats mono-1T at month scale, though its
  optimum moved from T=4 to T=8 (T=2 127.4, T=4 106.1, T=8 99.4 s) for the same
  reason: more write work per thread rewards more threads.
- **The merge is now 70% of MAS N=16's wall time** (64.0 s of 91.2 s), up from
  67%. The Amdahl wall of spec §14 Q4's "per-worker single-writer stores, merge
  at the sink" did not move; it got taller. `merge_from` is N sequential
  `INSERT OR IGNORE` passes, each probing the UNIQUE index once per row against
  a growing destination — ~22M probes that, sources now being disjoint, almost
  never find anything. Branch `perf/merge-set-based` replaces them with one
  hash-based `DISTINCT` over the union; **not yet measured**, because measuring
  it during this sweep would have falsified both.
- **`events_per_s` counts processed events**, identical input work for every
  config. Row persistence now equals it exactly — nothing is dropped between
  extraction and the store.
- **Initial dispatch is gated on worker registration** (`--workers N`): without
  the gate, ZMQ PUSH's slow-joiner behavior queued every file into the first
  connected worker and MAS timings measured a serialized pipeline (sweep #1,
  discarded).
- **`rows_per_s` uses a nominal 86,399 rows/day**; real days range 58,772 to
  86,401 raw rows, so it is approximate for the 7- and 28-day volumes —
  `events_per_s` is the measured throughput.
- **MAS `clean_s` includes worker spawn, ZMQ connect, and the registration
  wait**. Empirically small — v=1 clean_s is flat ~3.2-3.4 s across N=1..16 —
  but it is part of the measured number.

### Measurement integrity

- The two series are not a controlled comparison. Same machine and same
  harness, but the disk was in a different state (2.6 GB freed before this run)
  and it is a laptop without fan control. Shape — how it scales with N, where
  the wall sits — is reliable; absolute seconds across the two runs are
  indicative.
- The 30 MAS rows at volumes 1 and 7 were re-measured separately: during the
  first pass `build/` was rebuilt underneath the running sweep while preparing
  `perf/merge-set-based`, so some of those runs may have exercised the
  set-based merge. They were re-run through the same harness with the restored
  binaries (`run_bench.sh --only mas --volumes "1 7"`, added for this purpose)
  and spliced in. The monolith block finished before any rebuild and the MAS
  28-day block ran after the binaries were restored; neither was affected. The
  superseded file is kept as `bench/results.csv.contaminated`.

### merge_all: measured end to end (2026-08-11, branch `perf/merge-set-based`)

The table above is this branch's sweep. The comparison is against `main`'s, which
is the same harness and the same binaries but for `merge_all`.

**Validity gate first.** `mono-1T` writes straight to the destination and has no
merge phase, so `merge_all` cannot reach it: 101.0 s → 101.8 s, **+0.8%**. Had it
moved, something other than the change under test did, and none of the rest would
be worth quoting.

28-day medians:

| arch | merge before | merge after | total before | total after | vs `mono-1T` |
|---|---:|---:|---:|---:|---:|
| mono-1T | — | — | 101.0 | 101.8 | 1.00x |
| mono-MT T=8 | 67.8 | 32.4 | 99.4 | 76.3 | 1.34x |
| mas N=8 | 62.8 | 25.5 | 92.6 | 58.1 | 1.75x |
| mas N=16 | 64.0 | 25.4 | 91.2 | **55.4** | **1.84x** |

**The scaling wall moved: 1.11x → 1.84x** against the sequential baseline.

Three things the projections got wrong, recorded because they were written down
before the measurement:

- **2.89x on the merge was the best case, not the case.** That A/B merged 8
  sources in isolation. In the sweep the merge improves ~2.1x consistently
  (mono-MT 2.09-2.25x, MAS 2.15-2.52x). Extrapolating the most favourable
  configuration to all of them was the error.
- **`mas N=1` gains nothing, by construction.** `merge_all` returns to
  `merge_from` for a single source, so its 0.92x is the same code path measured
  twice, not a regression.
- **The architectures do not converge.** A projection assuming a flat 22.8 s
  merge everywhere put mono-MT T=8 and MAS N=16 within ~4 s. Measured, the gap is
  20.9 s, because MAS's merge benefits more (25.4 s) than mono-MT's (32.4 s).

**1.84x is a lower bound.** `clean_s` came out higher on this branch than on
`main` for every parallel configuration (+7% to +34%, unevenly). `merge_all` does
not touch the clean phase, and MAS `clean_s` is documented above as including
worker spawn, ZMQ connect and the registration wait — a jittery component. So the
new totals carry inflated clean time; with clean at `main`'s levels the ratio
would be higher, not lower. The inflation is unexplained and is the reason the
figure is presented as a floor.

**Measurement caveat.** The first attempt at this sweep was killed by SIGTERM at
65 of 81 rows, mid-MAS. The monolith block had completed and no binary changed
during it, so those 36 rows were kept and the MAS block was re-run through the
same harness (`run_bench.sh --only mas`). The two blocks therefore come from
different sessions on the same machine — the same limitation already noted for
the 30 re-measured rows in the previous sweep.

## Parquet vs DuckDB: where the persistence cost actually goes (2026-08-13, branch `feat/parquet-store`)

The store was 79.8% of `mono-1T`'s wall-clock, so the Parquet backend exists to
ask whether removing the index, the WAL and the per-row constraint is worth what
it costs. It is not a migration: DuckDB remains the default, and this section
records the measurement that says why.

Both stores were built from the same 28 day-files of February 2026, `machine_id`
`MCC`, one thread, and they agree exactly: **21,872,663 events each**. The
Parquet side holds 21,872,663 rows before deduplication as well, so no worker
re-dispatch occurred and the read-side `DISTINCT ON` removes nothing — it is
paid for in full anyway, which is the point of the read table below.

### Write

| backend | wall | store on disk |
|---|---:|---:|
| Parquet | **33.7 s** | 233 MB (28 files) |
| DuckDB | 90.9 s | 1183 MB |

**Parquet writes the month 2.70x faster** (90.9 / 33.7) and the store is 5.07x
smaller (1183 / 233 MB).

### Read — median of 3, the three canned reports over the whole month

| report | DuckDB | Parquet | ratio |
|---|---:|---:|---:|
| kpi | 2.188 s | 9.675 s | 4.42x |
| drift | 1.293 s | 8.365 s | 6.47x |
| anomalies | 1.578 s | 8.496 s | 5.38x |
| **all three** | **5.059 s** | **26.536 s** | **5.25x** |

### The net, and it is a loss

Writing the month once saves 57.2 s. Every subsequent run of the three reports
costs an extra 21.5 s. **The saving is gone after 2.7 report runs** (57.2 /
21.5), and the store is written once and read for the rest of the project.

**DuckDB wins.** Parquet wins the phase this branch set out to optimise and
loses the one that follows it.

### What the read costs are actually made of

One `GROUP BY head_id` aggregate over the full 21.9M rows, median of 3, isolates
the layers:

| what is read | median |
|---|---:|
| DuckDB native table | 0.047 s |
| Parquet, plain scan, no dedup | 0.081 s |
| Parquet + `DISTINCT ON` | 0.592 s |
| Parquet + `DISTINCT ON` + `ORDER BY` (the shipped view) | 1.514 s |

**Parquet-the-format is not the problem** — a plain scan is within 1.7x of the
native table. The 32x comes from what replaced the write-time UNIQUE index:
`DISTINCT ON` costs 7.3x the plain scan, and the `ORDER BY` that makes it
deterministic (without it DuckDB compiles it to `HASH_GROUP_BY` + `first()`,
where which row survives is undefined) costs another 2.6x on top. Both are
required for correctness and neither can be dropped.

So the finding is not "Parquet is slow". It is that **moving idempotency from
write time to read time moves the cost and multiplies it**, because the write
happens once and the read happens on every query.

### Measurement integrity

- **The write regime was forced by disk, and it favours DuckDB, not Parquet.**
  The plan called for one `mas_monolith` invocation per backend over the 28
  extracted files. That needs the 1.5 GB CSV pool plus a 1.26 GB DuckDB store
  present at once, against 652 MB free. So each day was extracted from the zip,
  fed to both backends, and deleted — 28 invocations per backend, identically
  for both.
- That regime was then calibrated rather than assumed harmless: days 01-04 were
  run both ways, twice. Four invocations vs one costs **DuckDB 1.28x and 1.26x**
  (12.92 s vs 16.51 s; 14.49 s vs 18.20 s) and Parquet nothing outside noise
  (4.59 vs 4.55 s; 6.05 vs 4.93 s). A single-invocation month would therefore
  put DuckDB near 115 s, not 90.9 s. **2.70x on write is a lower bound.**
  The mechanism was not investigated; only the direction and size matter here,
  and both were reproduced.
- The read timings include the view's full `ORDER BY` sort on every query.
  Period predicates do not push past it: the whole matched corpus is sorted
  regardless of the filter.
- Same machine and same session for every figure in this section, unlike the two
  sweeps above.
