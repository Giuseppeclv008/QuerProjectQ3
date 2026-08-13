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
- **The architectures do not converge.** ~~A projection assuming a flat 22.8 s
  merge everywhere put mono-MT T=8 and MAS N=16 within ~4 s. Measured, the gap is
  20.9 s, because MAS's merge benefits more (25.4 s) than mono-MT's (32.4 s).~~
  **[WITHDRAWN 2026-08-13 — see below.]** Interleaved on the same machine the gap
  is −2.1 s and its sign flips round to round. Both this claim and the ~4 s
  projection it corrected were reading noise.

**~~1.84x is a lower bound.~~ Superseded 2026-08-13: it is a number with tens of
percent of uncertainty.** The original reasoning ran: `clean_s` came out higher
on this branch than on `main` for every parallel configuration (+7% to +34%,
unevenly) and for none of mono-1T, `merge_all` cannot touch the clean phase, so
the totals carry inflated clean time and the true ratio can only be higher.

That inference does not hold, and an interleaved A/B says why. Four rounds of
mono-1T, mono-MT T=8 and MAS N=16 over the same 28 day-files, one binary set,
twenty minutes: mono-1T's `clean_s` spread 3% while mono-MT spread 21% and MAS
53%, both climbing round on round. This is a `Mac14,2` — a MacBook Air M2, with
**no fan**. A parallel configuration's `clean_s` here records when in the sweep it
ran, not how fast it is, and the sign of the difference between two sweeps is set
by run order rather than by the code under test. Nothing was inflated *by the
branch*; the two sweeps sampled different points on a thermal curve.

The same effect explains the 20.9 s gap withdrawn above. Per the measurement
caveat below, mono-MT's rows come from the tail of a long hot run and MAS's from
a fresh session after the SIGTERM — and mono-MT's recorded `clean_s` of 43.31 is
correspondingly high against 33.9 interleaved, MAS's 29.27 correspondingly low
against 38.8. The caveat was recorded; the conclusion was drawn across it anyway.

Full measurement in [`docs/validation-log.md`](../validation-log.md), entry
2026-08-13. What survives: every correctness result, `merge_all`'s 2.89x on its
own benchmark, the structural finding that merge cost stopped growing with source
count, and mono-1T's timings. What does not: any end-to-end ratio quoted to three
significant figures, and any comparison between two parallel architectures
measured in different sessions. Read the table below as shape, not as seconds,
until the sweep is repeated on hardware with active cooling.

**Measurement caveat.** The first attempt at this sweep was killed by SIGTERM at
65 of 81 rows, mid-MAS. The monolith block had completed and no binary changed
during it, so those 36 rows were kept and the MAS block was re-run through the
same harness (`run_bench.sh --only mas`). The two blocks therefore come from
different sessions on the same machine — the same limitation already noted for
the 30 re-measured rows in the previous sweep.

### What the CUDA speedup is worth end to end (Amdahl)

The clean-phase numbers are large and the end-to-end number is not, and the gap
between them is the finding.

**Correction (2026-08-13, review): the recorded CUDA `clean_s` measures less
work than every other contender's.** The seven stage timers stopped before the
loop that materializes the `CapEvent` vector — the "materialize events in
memory" half of spec §6.1's clean mode — and before `check_header`, the pinned
allocation and every `cudaMalloc`. A host-side replica of the materialize loop
costs about as much as the entire recorded number at both 1 and 28 day-files,
and the recorded rows agree: the 28-day CUDA row carries 8.5 s of process CPU
against 1.8 s of reported clean. The code now times an eighth `materialize_s`
stage and reports the process wall clock alongside; **the CUDA rows below are
best read as roughly half the comparable number** until the sweep is re-run
with the corrected timers. The CPU and Python rows measure their whole work
and stand as recorded.

28-day results on the Windows target box (RTX 4070 Laptop, CUDA 13.3) — median
of 3, with the min–max spread, because n=3 on a fanless-adjacent laptop does
not support three significant figures (the same lesson the entry above records
for the M2):

| arch | clean 28d, median [min–max] | vs CUDA as recorded | vs CUDA, window-corrected (est.) |
|---|---:|---:|---:|
| **cuda** | **1.82 s** [1.81–3.89] | — | ~3.6 s |
| cpp-MT (8 threads) | 7.68 s [7.62–7.73] | 4.2x | **~2x** |
| cpp-1T | 46.77 s [46.60–47.11] | 25.7x | **~13x** |
| py-numpy | 91.00 s [89.79–91.50] | 50x | ~25x |

The CUDA spread is itself the caveat in miniature: repeat 1 measured 3.89 s —
2.15× the other two repeats (cold file cache, plus `--verify`'s CPU load in
the same process) — so the median sits one outlier away from doubling.

`mono-1T` end to end is **230.5 s** [225.3–231.2] against 46.5 s of clean, so
persistence costs **~184 s — around 80% of wall-clock**. Substituting a faster
clean leaves that untouched:

| | clean + store | e2e vs mono-1T |
|---|---:|---:|
| cpp-1T | 46.8 + 184 = 230.7 s | 1.00x |
| cpp-MT 8T | 7.7 + 184 = 191.6 s | 1.20x |
| cuda (window-corrected est.) | ~3.6 + 184 = ~187.5 s | **~1.23x** |

**CUDA against the 8-thread C++ already in the project: ~2x on the clean phase
becomes ~1.02x end to end.** The measurement-window correction halves the
clean-phase ratios and moves the end-to-end conclusion by one point — which is
the point: the conclusion never depended on the flattered number.

This is Amdahl applied honestly. Speeding a phase that is ~20% of the total
even by 13x yields at most ~1.25x, and ~1.23x is the estimate. The stage
breakdown (pre-correction, so read it as shape) says the same thing from
inside: of the recorded 1.82 s, 1.17 s is disk read and ~0.29 s is GPU
compute. The kernel stopped being the bottleneck before the pipeline did.

**The defensible claim is not "the GPU makes cleaning faster" — it is that
cleaning has stopped being the problem.** Three independent paths were measured
— multithreaded CPU, distributed MAS, GPU — and all three land on the same
place: the cost is persistence, not transformation. `merge_all` attacked it
from one side (2.89x on the merge in isolation); CUDA proved it from the
other, by driving GPU compute to ~0.29 s and moving the total only to ~1.23x.

Two qualifications the first version of this section did not carry:

- The clean-phase ratios are also a statement about the CPU baseline.
  `cpp-1T` parses at ~35 MB/s because `CsvRawReader` builds an
  `std::istringstream` per row and calls `std::stod` 108 times per row; a
  `std::from_chars` parser over the same buffer would plausibly close much of
  the gap on its own. "~13x over C++ 1T" measures the distance between a tuned
  GPU pipeline and an untuned CPU parser — which strengthens, not weakens, the
  persistence conclusion: with a competent CPU parser the clean phase shrinks
  further below the store cost.
- Every number in this section is n=3 on a laptop. The re-run with the
  corrected timers (tracked in the validation log) is what turns the
  window-corrected column from an estimate into a measurement.

It also settles the question the kernel was written to answer: **how much
headroom was left in the clean phase? Almost none, and that is now measured
rather than assumed.**
