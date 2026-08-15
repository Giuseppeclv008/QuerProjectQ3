# Benchmark results (median of 3 repeats)

| arch | n_workers | threads | files | clean_s | merge_s | total_s | events_per_s | peak_rss_mb | cpu_pct |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| mas | 1 | 1 | 1 | 18.939 | 15.067 | 34.008 | 22515.6 | 203.5 | 186.8 |
| mas | 1 | 1 | 7 | 94.042 | 76.042 | 169.857 | 22966.5 | 655.9 | 190.6 |
| mas | 1 | 1 | 28 | 514.587 | 430.401 | 944.32 | 23162.3 | 3197.5 | 192.9 |
| mas | 2 | 1 | 1 | 19.031 | 2.614 | 21.675 | 35326.9 | 290.4 | 124.9 |
| mas | 2 | 1 | 7 | 49.726 | 11.951 | 61.544 | 63385.8 | 975.2 | 234.9 |
| mas | 2 | 1 | 28 | 279.52 | 69.076 | 348.14 | 62827.2 | 4460.3 | 238.4 |
| mas | 4 | 1 | 1 | 19.001 | 2.576 | 21.632 | 35397.1 | 340.2 | 124.6 |
| mas | 4 | 1 | 7 | 37.776 | 12.031 | 49.838 | 78273.9 | 1073.4 | 312.5 |
| mas | 4 | 1 | 28 | 151.484 | 68.621 | 219.834 | 99496.3 | 4287.9 | 439.8 |
| mas | 8 | 1 | 1 | 19.071 | 2.741 | 21.812 | 35105 | 427.9 | 127.5 |
| mas | 8 | 1 | 7 | 24.763 | 11.366 | 36.295 | 107481 | 1192.2 | 516.9 |
| mas | 8 | 1 | 28 | 111.884 | 70.814 | 182.591 | 119790 | 4577.6 | 721.4 |
| mas | 16 | 1 | 1 | 19.262 | 2.665 | 21.993 | 34816.1 | 607 | 134.7 |
| mas | 16 | 1 | 7 | 24.942 | 11.513 | 36.482 | 106930 | 1376.8 | 515.1 |
| mas | 16 | 1 | 28 | 74.9 | 64.847 | 140.438 | 155746 | 4858.6 | 1488.1 |
| mono-1T | 0 | 1 | 1 | 18.441 | 0 | 18.441 | 41522.2 | 71.6 | 106 |
| mono-1T | 0 | 1 | 7 | 97.617 | 0 | 97.617 | 39962.5 | 103.4 | 106.6 |
| mono-1T | 0 | 1 | 28 | 537.787 | 0 | 537.787 | 40671.6 | 220.5 | 111.2 |
| mono-MT | 0 | 2 | 1 | 18.618 | 2.46 | 21.105 | 36281 | 191.6 | 110.5 |
| mono-MT | 0 | 2 | 7 | 48.937 | 11.868 | 60.961 | 63992 | 796.9 | 196.5 |
| mono-MT | 0 | 2 | 28 | 264.91 | 72.184 | 338.338 | 64647.4 | 3371.6 | 197 |
| mono-MT | 0 | 4 | 1 | 18.61 | 2.398 | 21.008 | 36448.5 | 193.2 | 107.2 |
| mono-MT | 0 | 4 | 7 | 28.086 | 11.185 | 39.264 | 99353.5 | 733.4 | 307.2 |
| mono-MT | 0 | 4 | 28 | 134.907 | 69.672 | 204.579 | 106916 | 3569.8 | 317.7 |
| mono-MT | 0 | 8 | 1 | 18.731 | 2.402 | 21.16 | 36186.7 | 199.2 | 109.6 |
| mono-MT | 0 | 8 | 7 | 26.828 | 11.439 | 38.301 | 101852 | 731 | 393.7 |
| mono-MT | 0 | 8 | 28 | 86.043 | 70.592 | 157.347 | 139009 | 3855.7 | 521 |

Caveats: measured on an actively-cooled i7-13700H laptop (6P+8E cores, 20 threads — N=16 exceeds the P-cores but not the hardware threads), median-of-3, merge phase reported separately; mono-MT uses a std::thread atomic-counter pool (dynamic load balancing, slightly fairer than PUSH/PULL round-robin).

## Data notes (resweep 2026-08-13: same month, actively-cooled hardware)

The table above is the full 81-run matrix re-measured on a machine that can
hold its clock: an HP Victus 16 (i7-13700H, 6P+8E cores / 20 threads, 16 GB,
active cooling, on AC). It replaces the `Mac14,2` series — a fanless MacBook
Air M2 whose parallel `clean_s` spread 21–53% across four interleaved rounds
of the same binary (validation log, 2026-08-13), which is what forced this
re-measurement. Every prior figure quoted from the M2 sweeps survives only as
history; the reference numbers are now these.

- **Repeatability is back, and that is the headline.** Across the 3 repeats of
  every 28-day configuration, `clean_s` spreads 0.1–1.8% and `total_s` spreads
  0.3–4.8% (worst: MAS N=16 at 4.8%). On the M2 the same harness spread 21%
  (mono-MT) and 53% (MAS) — there a number recorded *when in the sweep it ran*;
  here it records the code.
- **One sweep, one session, for the first time.** All 81 rows come from a
  single uninterrupted pass: binaries built before the run and untouched during
  it, no SIGTERM splice, no re-measured blocks — the two prior sweeps each
  carried one of those caveats. Every run matched the oracle exactly:
  21,872,663 distinct `(head_id, ts)` events over 28 days, 3,901,017 over 7,
  765,711 over 1, persisted 1:1 (the `ts` identity of 2026-08-11 holds; nothing
  is dropped between extraction and the store).
- **The comparison is like-for-like only since this sweep.** `run_bench.sh`
  never passed a machine id to workers, so MAS stores carried the 3-char argv
  default `MCC` while the monolith wrote the full 35-char id — the first column
  of `UNIQUE(machine_id, head_id, ts)`, written 21.9M times. On the M2 this was
  timing-neutral (at v=1, mono and MAS clean within noise of each other). On
  this box it is not: a 35-char VARCHAR leaves DuckDB's inline-string
  representation, and `clean.exe` on one day-file goes from 9.1 s (`MCC`) to
  18.3 s (full id) with nothing else changed. The harness now passes the real
  id everywhere; without that fix the sweep would have handed MAS a 2× head
  start on the clean phase and measured a default argument.
- **The merge cost is flat across N≥2 — reconfirmed on a second platform.** At
  28 days: 69.1 / 68.6 / 70.8 / 64.8 s for N=2/4/8/16, and 72.2 / 69.7 / 70.6 s
  for mono-MT T=2/4/8. `merge_all`'s set-based pass costs what the volume
  costs, not what the partitioning costs. The N=1 row is the control:
  `merge_all` falls back to the per-row `merge_from` path for a single source,
  and that path costs **430.4 s here — 6.2× the set-based pass** over the same
  total volume. On the M2 the same fallback was ~10% over set-based. Per-row
  index probing is exactly the kind of work this platform taxes, which makes
  the case for the set-based merge stronger, not weaker.
- **The merge is 46% of MAS N=16's wall, and that is unchanged from the M2.**
  64.8 s of 140.4 s here; 25.4 s of 55.4 s there, i.e. 45.8%. The "down from
  70%" this bullet first claimed compared against the M2's *pre-*`merge_all`
  sweep, where the merge was the old per-row `merge_from` — a different
  algorithm, not a different machine. Like for like, with `merge_all` on both,
  clean and merge scale between the two platforms by the *same* factor at
  N=16 (29.3 → 74.9 s and 25.4 → 64.8 s, both 2.56×), which is exactly why the
  fraction does not move. The set-based pass is not platform-neutral either: it
  costs 2.56× more here, the same as everything else.
- **MAS `clean_s` still includes worker spawn, ZMQ connect, and the
  registration wait** — measurably ~0.5 s here (v=1: MAS 19.0–19.3 s against
  mono-1T's 18.4 s). Initial dispatch stays gated on `--workers N`
  registrations; the slow-joiner capture that flattened sweep #1 cannot recur.
- **`rows_per_s` uses a nominal 86,399 rows/day** as before; `events_per_s` is
  the measured throughput (mono-1T 40.7k/s, MAS N=16 155.7k/s at 28 days).

### The measured ratio (closes "1.84× is a lower bound")

The M2 sweep measured MAS N=16 end-to-end at 1.84× mono-1T, argued it was a
floor, then withdrew the argument when the interleaved A/B showed tens of
percent of thermal uncertainty on every parallel configuration. This sweep
replaces that number with a measurement:

| 28-day medians | total_s | spread over 3 reps | vs mono-1T |
|---|---:|---:|---:|
| mono-1T | 537.8 | 1.1% | 1.00× |
| mono-MT T=8 | 157.3 | 1.1% | **3.42×** |
| mas N=8 | 182.6 | 1.5% | 2.95× |
| mas N=16 | 140.4 | 4.8% | **3.83×** |

**MAS N=16 runs the month end-to-end at 3.83× the sequential baseline**
(537.8 s → 140.4 s), with the ratio's inputs repeatable to 1.1% and 4.8%. The
clean phase alone parallelizes at 7.2× (537.8 → 74.9 s across 16 workers on 20
hardware threads).

3.83× is not "the true value of 1.84×", but the reason is not a different cost
mix — the mix is the same. With `merge_all` on both machines the merge is 46% of
MAS N=16's wall on either, and clean and merge scale between them by an
identical 2.56×. What differs is how far apart the two *ends* of the ratio move:

| 28-day median | M2 | this box | factor |
|---|---:|---:|---:|
| mono-1T (the baseline) | 101.8 | 537.8 | **5.28×** |
| MAS N=16 (the parallel end) | 55.4 | 140.4 | **2.54×** |
| ratio | 1.84× | 3.83× | |

The baseline degrades by 5.28× while sixteen workers degrade by only 2.54×,
because this box answers a more expensive per-row path with 20 hardware threads
against the M2's 8. The speedup therefore measures the machine's serial
penalty as much as the design's parallel efficiency, and neither 1.84× nor
3.83× transfers to a third machine. Quote the ratio with the box attached.

What this measurement does settle is the part the M2 could not: on hardware that
holds its clock, the end-to-end gain is real and repeatable — inputs stable to
1.1% and 4.8% — rather than an artifact of run order.

### The mono-MT vs MAS gap, measured clean

The 20.9 s gap recorded by the PR #9 sweep was inflated twice over: mono-MT's
merge clock included deleting its own per-thread stores (fixed in `638478b`,
before this sweep) and its rows came from a hotter part of the M2's thermal
curve than MAS's (the SIGTERM-split sessions). The interleaved A/B then showed
the two architectures tied on that machine, gap −2.1 s with the sign flipping
round to round.

Measured here, with both clocks stopping at the same place and 1.1–4.8%
spreads: **the gap is real but belongs to parallelism, not to process
isolation.** At equal parallelism MAS loses — N=8 trails mono-MT T=8 by 25.2 s
(182.6 vs 157.3), the cost of 8 processes, ZMQ transport and per-worker stores
against 8 threads on an atomic counter. MAS's win at the top of the matrix
(140.4 vs 157.3, 16.9 s) is bought by N=16, a parallelism the thread pool was
not swept to. Same file-grain work, same store strategy: threads are the
cheaper vehicle at like-for-like N on this box.

### Measurement integrity

- **Hardware:** HP Victus 16-r0xxx, Intel i7-13700H (6 P-cores + 8 E-cores, 20
  threads, up to 5.0 GHz), 16 GB DDR5, NVMe SSD, **active cooling** (dual fan),
  on AC power, Windows "Balanced" plan, otherwise idle. The RTX 4070 Laptop GPU
  is present but unused — this is the CPU build. This sweep exists because the
  previous machine was a fanless MacBook Air M2 whose thermal accumulation made
  parallel timings a function of run order; on this chassis the spreads above
  say that effect is gone.
- **Toolchain:** MSVC 19.41 `/O2 /Ob2 /DNDEBUG` (VS 2022, Release), DuckDB
  v1.2.2 official `windows-amd64` binary, libzmq 4.3.5 built from source —
  first time on Windows for this repo; all 85 unit tests pass. Harness = Git
  Bash + `bench/win_time.cpp` (QPC + `GetProcessTimes` +
  `PeakWorkingSetSize`), which prints the two lines `parse_time()` reads from
  BSD `time -l`.
- **Absolute seconds do not transfer between this table and any Mac figure.**
  mono-1T's month is 537.8 s here against ~101–108 s on the M2 — same source,
  both Release: the per-row store path (out-of-line VARCHAR machine id, MSVC
  stdlib parsing) is ~5× dearer on this platform, the DuckDB-internal merge is
  not. Cross-machine comparisons of shape are fine; cross-machine comparisons
  of seconds are not the deliverable of this file.
- **History:** the M2 series (its identity-fix before/after, the merge_all
  A/B and its 2.89× isolated merge measurement, the thermal post-mortem) is in
  [`docs/validation-log.md`](../validation-log.md), entries 2026-08-11 through
  2026-08-13. The 2.89× like-for-like merge comparison stands as measured — it
  alternated binaries within one session — and its effect is visible here as
  the 6.2× N=1-fallback delta above.
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

**Correction (2026-08-13, review): the recorded CUDA `clean_s` measured less
work than every other contender's.** The seven stage timers stopped before the
loop that materializes the `CapEvent` vector — the "materialize events in
memory" half of spec §6.1's clean mode — and before `check_header`, the pinned
allocation and every `cudaMalloc`. The code now times an eighth
`materialize_s` stage and reports the process wall clock alongside.
Re-measured the same day on the RTX box (below), the correction turned out
*larger* than the review's "roughly half" estimate: at 28 day-files the
materialize loop alone is 4.64 s against 1.80 s for the seven original stages
— the untimed part was ~72% of the corrected number, not ~50%. (The seven
stages still summing to 1.80 s also says the old 1.82 s recording was
accurate for what it measured; it measured about a quarter of the phase.) The
CPU and Python rows always measured their whole work.

28-day results on the Windows target box (RTX 4070 Laptop, CUDA 13.3), sweep
of 2026-08-13 with the corrected timers — median of 3, with the min–max
spread, because n=3 on a laptop does not support three significant figures
(the same lesson the entry above records for the M2):

| arch | clean 28d, median [min–max] | vs CUDA |
|---|---:|---:|
| **cuda** | **6.43 s** [6.33–8.34] | — |
| cpp-MT (8 threads) | 8.21 s [8.12–8.32] | 1.3x |
| cpp-1T | 46.26 s [45.91–46.57] | 7.2x |
| py-naive | 74.64 s [74.32–76.31] | 11.6x |
| py-numpy | 85.82 s [84.53–86.99] | 13.4x |

(py-naive is measured at every volume now that the extrapolation machinery is
gone — and the estimate that machinery was built on measured true: "~75 s per
28-day repeat" came out 74.6 s median.)

Repeat 1 is still the caveat in miniature: its `clean_s` reads 8.34 s (cold
file cache) and its *wall* reads 58.9 s, because `--verify` runs the full CPU
differential in the same process on the first repeat. The median absorbs
both; the spread is why the interval is published. The CUDA context and
allocations sit outside `clean_s` and inside the wall clock, where spec §6.1
puts them: total 8.43 s median against 6.43 s clean.

`mono-1T` end to end is **257.6 s** [254.5–266.6] against 45.5 s of
store-free clean, so persistence costs **~212 s — around 82% of wall-clock**.
Substituting a faster clean leaves that untouched:

| | clean + store | e2e vs mono-1T |
|---|---:|---:|
| cpp-1T | 46.3 + 212.1 = 258.4 s | 1.00x |
| cpp-MT 8T | 8.2 + 212.1 = 220.3 s | 1.17x |
| cuda | 6.4 + 212.1 = 218.5 s | **1.18x** |

**CUDA against the 8-thread C++ already in the project: 1.3x on the clean
phase becomes ~1.01x end to end.** The measurement-window correction shrank
the clean-phase ratios by more than the review's halving estimate (cpp-MT
~2x estimated, 1.3x measured; cpp-1T ~13x estimated, 7.2x measured) and moved
the end-to-end number from ~1.23x to a measured 1.18x — which is the point:
the conclusion never depended on the flattered number.

This is Amdahl applied honestly. Speeding a phase that is ~18% of the total
by 7.2x yields 1.18x, and 1.18x is what is measured. The stage breakdown, now
with nothing left outside it, says the same thing from inside: of the 6.43 s,
4.64 s is host-side event materialization, 1.20 s is disk read, 0.32 s is
PCIe transfer both ways, and ~0.29 s is GPU compute (index + parse + delta +
compact — the pre-correction "~0.29 s" estimate of the kernels' cost held
exactly). The kernel stopped being the bottleneck before the pipeline did —
and, measured, so did the rest of the GPU path: what remains is a
single-threaded, allocation-bound host loop.

**The defensible claim is not "the GPU makes cleaning faster" — it is that
cleaning has stopped being the problem.** Three independent paths were measured
— multithreaded CPU, distributed MAS, GPU — and all three land on the same
place: the cost is persistence, not transformation. `merge_all` attacked it
from one side (2.89x on the merge in isolation); CUDA proved it from the
other, by driving GPU compute to ~0.29 s and moving the total only to 1.18x.
The re-run also put a measured number where a hardcoded zero had been:
mono-MT's 28-day merge is 58.7 s [57.7–62.5], not the 0.000 the driver used
to write — and not the ~150 s the review guessed while flagging the hardcode.

Two qualifications this section keeps:

- The clean-phase ratios are also a statement about the CPU baseline.
  `cpp-1T` parses at ~35 MB/s because `CsvRawReader` builds an
  `std::istringstream` per row and calls `std::stod` 108 times per row; a
  `std::from_chars` parser over the same buffer would plausibly close much of
  the gap on its own. "7.2x over C++ 1T" measures the distance between a tuned
  GPU pipeline and an untuned CPU parser — which strengthens, not weakens, the
  persistence conclusion: with a competent CPU parser the clean phase shrinks
  further below the store cost. And the same reading now applies to the GPU
  row itself: 72% of its clean time is a single-threaded host loop building
  strings.
- Every number in this section is n=3 on a laptop with ordinary desktop
  background load. The intervals are the honest resolution; the medians are
  the claim.

It also settles the question the kernel was written to answer: **how much
headroom was left in the clean phase? Almost none, and that is now measured
rather than assumed.**


## Parquet vs DuckDB: where the persistence cost actually goes (2026-08-14, branch `feat/parquet-store`)

The Parquet backend exists to ask one question: the DuckDB store maintains a
UNIQUE index per row, a write-ahead log and a checkpoint — is removing them
worth what it costs? It is not a migration. DuckDB remains the default and the
persistent format, and Parquet's supported use is `mas_export`.

One `mas_monolith` invocation per backend over the same 28 day-files of
February 2026, `machine_id` `MCC`, one thread. Both stores hold **21,872,663
events**, raw equal to distinct.

Every figure has an artifact in `bench/parquet-comparison/`:
`write_single.out` and `write_single_duckdbfirst.out` (the two orderings),
`parity.out`, `decompose_final.out` (median of 7), `calibrate.out`,
`write_raw_perday.csv` (the superseded per-day regime, kept as an independent
estimate), `run_bench_smoke.csv` and `run_bench_smoke_posthoist.csv` (the 1-day
`run_bench.sh` parquet block, either side of the Appender hoist),
`bench/read_results.csv` (the 18 read repeats) and
`bench/read_results_with_sort.csv` (the same, through the withdrawn `ORDER BY`).

### Write

| backend | DuckDB first | Parquet first | store on disk |
|---|---:|---:|---:|
| Parquet | **35.48 s** | 34.02 s | 233.4 MB (28 files) |
| DuckDB | 98.96 s | 98.86 s | 1182.8 MB |
| ratio | **2.79x** | 2.91x | 5.067x smaller |

Both store sizes come from `decompose_final.out`, which sums `getsize` over the
files. An earlier revision took the DuckDB half from `write_single.out`'s `du
-sk` instead (1183.6 MB) — same pair of stores, two tools, and nothing that
moved the ratio, but one row should not be sourced from two runs.

Run in both orders because this volume slows DuckDB's larger sequential writes
when free space runs low, and each backend's store eats the space the next one
sees. **2.79x is the ordering that does not flatter Parquet, and the one to
quote**; the 4% spread between the orderings is the size of that effect. It is
*not* "the counterbalanced figure", as this section used to call it — a
counterbalanced estimate averages the orderings (~2.85x); 2.79x is deliberately
the conservative one instead.

**Each cell above is a single invocation** — n=1 per ordering, against the n=3
this document holds itself to in the CUDA section above. A month-scale store
does not fit on this volume three times over, which is the reason rather than
an oversight. What stands in for the missing repeats is three further estimates
of the same ratio, none of which the headline depends on but all of which agree
with it:

| estimate | artifact | ratio |
|---|---|---:|
| month, one invocation per backend, duckdb-first | `write_single_duckdbfirst.out` | **2.79x** |
| month, one invocation per backend, parquet-first | `write_single.out` | 2.91x |
| 28 per-day invocations per backend (superseded regime) | `write_raw_perday.csv` | 2.69x |
| 1 day through `run_bench.sh`, n=3 per backend | `run_bench_smoke.csv` | 2.82x |

(That last row predates the Appender hoist described below, as do all the
month-scale figures. `run_bench_smoke_posthoist.csv` is the same run after it.)

**The Parquet write figures are a floor on speed, and the hoist that lowered
them cost memory.** `ParquetEventStore` built a fresh `duckdb::Appender` on
every `write()` — one catalog lookup and type bind per 8,192-event batch, about
2,670 of them per day-file. Hoisting it to one per store trades those for a
larger resident buffer, because rows now accumulate until DuckDB's
`DEFAULT_FLUSH_COUNT` (204,800) instead of landing in `buf` every batch. Both
halves are in `run_bench_smoke.csv` and `run_bench_smoke_posthoist.csv`:

| metric | pre-hoist | post-hoist | Δ | `mono-1T` control |
|---|---:|---:|---:|---:|
| parquet wall clock | 1.254 s | 1.195 s | **-4.7%** | +8.3% (drifted) |
| parquet peak RSS | 322.1 MB | 351.1 MB | **+9.0%** | -0.1% (flat) |

**The memory cost is the better-measured of the two.** The RSS ranges do not
overlap ([320.1, 322.8] against [347.4, 353.9]) and its control is flat; the
timing control moved by more than the timing effect, so -4.7% is a direction
and not a magnitude. Quoting the speed win without the memory regression would
have been reporting the favourable half of one artifact pair — which is the
thing the Measurement integrity section below exists to forbid.

All the month-scale figures above predate the hoist. And a store written to
measure what persistence costs was itself paying an avoidable tax while it
measured.

And `calibrate.out` supplies the within-condition spread the n=1 cells cannot:
its later pair repeats to within 0.9% (parquet 4.38/4.42 s, 0.91% of the
mean; duckdb 11.89/11.85 s, 0.34%).
Run-to-run noise is an order of magnitude below the effect, so the conclusion
survives the thin sampling even though the sampling should be stated.

### Read — the three canned reports over the whole month, 3 repeats

| report | DuckDB | Parquet | ratio |
|---|---:|---:|---:|
| kpi | 2.252 s | 5.304 s | 2.36x |
| drift | 1.323 s | 3.340 s | 2.52x |
| anomalies | 1.574 s | 3.760 s | 2.39x |
| all three, median of the 3 suite totals | **5.139 s** | **12.400 s** | **2.41x** |

Suite totals: DuckDB 5.113 / 5.139 / 5.448 s, Parquet 12.072 / 12.400 /
12.659 s.

**These are report wall times, not query times.** `read_bench.py` runs each
report as a subprocess, so every measurement carries a Python interpreter start
and the analytics package's imports; and unlike the write side, the backend
order within a repeat is fixed (DuckDB, then Parquet) rather than
counterbalanced. Both biases run the same way, and it is the safe way: a
constant added to both sides *shrinks* a ratio, so 2.41x understates the
query-only penalty and puts the break-even later than it truly is — against
this document's own conclusion rather than for it. `decompose_final.out` is the
query-only isolation for anyone who needs the unpadded number.

### The net

Writing the month once saves **63.48 s** (98.96 − 35.48). Every later run of the
three reports costs an extra **7.26 s** (12.400 − 5.139). **The saving is gone
after 8.7 report runs** (63.48 / 7.26).

**DuckDB still wins, but by far less than this section claimed a day ago, and
the reason is a defect that was in the measurement rather than in either
backend** — see the withdrawal below. Eight or nine report runs is a threshold a
working project crosses quickly and a one-off export never reaches, so the
honest statement is: **DuckDB for the store that gets queried, Parquet for the
copy that gets handed over.** That is what `mas_export` is for.

### What the read cost is made of

One `GROUP BY head_id` aggregate over all 21.9M rows, median of 7
(`bench/parquet-comparison/decompose.py`, output beside it):

| what is read | median | vs the layer above |
|---|---:|---:|
| DuckDB native table | 0.030 s | — |
| Parquet, plain scan, no dedup | 0.062 s | 2.05x |
| Parquet + `DISTINCT ON` (**the shipped view**) | 0.456 s | 7.38x |
| *withdrawn:* + `ORDER BY` | 1.358 s | 2.98x |

**Parquet-the-format is not the cost** — a plain scan is within 2.05x of the
native table. The 15.1x the shipped view pays is the `DISTINCT ON` that replaced
the write-time UNIQUE index. Moving idempotency from write time to read time
moves the cost and multiplies it, because the write happens once and the read
happens on every query. That finding survives; only its size changed.

### Withdrawn: the `ORDER BY` this view used to carry

Until 2026-08-14 the Parquet view ended in `ORDER BY machine_id, head_id, ts`,
justified in three documents as making `DISTINCT ON` deterministic — *"which row
of a duplicate group survives is genuinely undefined"* without it.

That justification was false, and a final review caught it:

- `EXPLAIN` puts `ORDER_BY` **above** `HASH_GROUP_BY`, so it runs after the
  group has been collapsed and cannot choose its survivor.
- The sort key was the `DISTINCT ON` key, so every row in a duplicate group
  compares equal on it — there is no tie to break even in principle.
- Adding it did change the surviving row in a two-file experiment (999 → 111),
  but ASC and DESC agreed with each other. What moved was the query plan, not a
  tie-break, and none of it was a guarantee.

And the guarantee was never needed. Duplicates arise only from a re-dispatched
work item, and those rows are byte-identical by construction — same input, same
extraction. Whichever survives, the content is the same.

**It cost 2.98x of the read path to defend a property that could not be provided
and was not required.** With it removed, the read penalty falls from 5.24x to
2.41x and the break-even moves from 3.0 report runs to 8.7. Every read figure in
this file's previous revision was measured through that sort.

That superseded data is committed as `bench/read_results_with_sort.csv`, not
merely described: the withdrawn 5.24x and the 3.0-run break-even recompute from
it exactly. The write side already kept its superseded regime this way
(`write_raw_perday.csv`), and a withdrawal a reader cannot check is asking for
the same trust the withdrawal was meant to stop asking for.

### Measurement integrity

- **The write was measured in both orderings** (above) rather than once, because
  the disk-pressure effect below is real and order-dependent.
- **A calibration that was itself measuring disk pressure, kept as a caution.**
  An earlier attempt could not run one invocation per backend — 652 MB free
  would not hold the 1.5 GB CSV pool and the 1.2 GB DuckDB store at once — and
  ran 28 invocations per backend instead. Calibrating that on days 01-04 with
  ~630 MB free said a single invocation cost DuckDB 1.28x and 1.26x. Repeated
  with 2.3 GB free it gave 0.99x and 1.00x: **it had been measuring free space,
  not invocation count.** Both pairs are in `calibrate.out`. The superseded
  per-day figures are `write_raw_perday.csv` (Parquet 33.75 s, DuckDB 90.88 s).
- Period predicates **do** push past the dedup: `EXPLAIN` puts `FILTER` below
  it, and a one-hour-scoped aggregate measured 0.027 s against 0.122 s for the
  full scan on a 2M-row table. An earlier revision of this file claimed the
  opposite. The reports are whole-month, so the committed numbers are unaffected.
- **The 79.8% that motivated this work was measured elsewhere.** It is 183.9 s
  of persistence in a 230.45 s `mono-1T` run on the RTX 4070 host. On this
  laptop the DuckDB month write is 98.96 s and `mono-1T` at 28 files is
  101.814 s in the table at the top of this file, which is why the plan's
  "expect DuckDB near 230 s" did not appear. The store's share on this laptop
  was not measured; that needs the null-store build. The comparison itself is
  unaffected — both backends, this laptop, one session, same input.
