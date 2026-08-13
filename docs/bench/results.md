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
