# Benchmark & Monolith Baseline — Design Spec

Date: 2026-07-10
Status: approved
Parent spec: `2026-07-04-iiot-data-refinement-mas-design.md` (§8, §9; Goal 5)
Predecessors: Plans 1–4 (cleaning core, DuckDB store, ZeroMQ runtime, resilience) — all merged.

## 1. Context & Problem

Goal 5 — "Demonstrate MAS scalability vs a monolithic baseline" — is the course's
graded core (§4: "brief demands parallelization + scalability proof"). The MAS side
is complete and validated on real data; what is missing is the experimental control
(a monolith), the second parallelism axis (§8's intra-process threading), and the
measurement apparatus (§9's harness, metrics, and plots).

Hardware reality this design targets: the development machine has 8 cores and
16 GB RAM; free disk is limited, and a full extracted month is ~1.6 GB of CSV.
The user frees disk before the sweep; the harness still guards.

## 2. Goals & Non-Goals

**Goals**

1. A monolithic baseline binary sharing the MAS's exact hot path, with a std::thread
   thread knob — the experimental control.
2. A reproducible sweep harness measuring both architectures across worker count,
   thread count, and data volume, with correctness asserted on every run.
3. The four §9 plot families plus a numbers table, committed as project evidence.

**Non-Goals (unchanged owners)**

- Docker packaging (§12 plan) — §9's "Dockerized reproducibility" lands there; this
  plan's reproducibility is the scripted harness on the dev machine.
- GPU (§8: optional stretch, explicitly poor fit for the dedup).
- KPI/anomaly agents, ingestion agent.
- Distributed multi-host runs (parent spec non-goal).

## 3. Key Decision — thread grain: files, not heads

Parent spec §8 names "OpenMP over 36 independent heads" as the intra-process axis.
This design deliberately deviates to **file-grain** intra-process threading and documents why:

- The hot path streams rows (`CsvRawReader::next` → `CapEventExtractor::process`);
  a row visits 36 heads with one integer compare each. Head-grain parallelism means
  forking/joining across 36 trivial compares per row (86,399 rows/day) — the work is
  memory-bound with negligible arithmetic intensity (§8 itself concedes this for
  GPU), so overhead would swamp any gain, and it would force restructuring a
  validated hot path.
- File-grain (a fixed pool of T `std::thread`s pulling day-files off an atomic counter, one `clean_file` per
  thread into a thread-local store, idempotent merge at the end) keeps the work
  grain **identical** to the MAS's unit of work. `mono-MT(T)` vs `MAS(N)` then
  differ only in mechanism — threads + shared-nothing stores vs processes + ZeroMQ
  — so their delta directly measures IPC/process overhead. A controlled experiment,
  not a conflated one.
- DuckDB stores are single-writer (store header; parent §14 Q4): per-thread store
  files merged by the existing `merge_from` is the same strategy the MAS sink uses.
  Symmetric, fair, zero new persistence semantics.

Consequence: the MAS gets no thread knob (a worker holds one file at a time); §9's
"threads/agent T" variable belongs to the monolith. Thread-safety argument: each
`clean_file` call constructs its own reader, extractor, and store — shared-nothing
by construction; correctness gate is count-invariance vs the sequential run.

## 3b. Repository Module Layout (precondition, standalone mini-PR)

Before this plan executes, `core/` is restructured into responsibility modules —
a pure mechanical move (zero logic change, suite stays green), merged as its own
PR so the benchmark diff stays clean:

```
core/include/mas/domain/     CapEvent, CapEventExtractor, Pipeline    (hot path)
core/include/mas/store/      EventStore, CsvEventStore, DuckDbEventStore, CsvRawReader
core/include/mas/agent/      Coordinator, CleaningWorker, Message
core/include/mas/transport/  Transport, ZmqTransport                  (DIP boundary)
core/src/<module>/           mirrored .cpp files
core/src/apps/               all CLI mains (clean, worker, coordinator, merge, monolith)
tests/, tests/fakes/         unchanged
```

Includes are nested and self-documenting (`#include "mas/agent/Coordinator.hpp"`).
CMake target names (`mas_core`, `mas_transport`) are unchanged. **Convention for
all future files**: new code lands in the module matching its responsibility; new
CLIs land in `core/src/apps/`; a file that fits no module is a design smell to
resolve before merging. This plan's `mas_monolith` lands in `core/src/apps/`.

## 4. Components

1. **`mas_monolith` CLI** (`core/src/apps/monolith_main.cpp`):
   `mas_monolith <out.duckdb> <machine_id> <threads> <day1.csv> [day2.csv ...]`
   - `threads == 1`: plain sequential loop (arch `mono-1T`).
   - `threads > 1`: std::thread pool (T threads, atomic file counter) over files, thread-local stores
     `<out>.tN.duckdb`, then `merge_from` each into `<out>` (arch `mono-MT`).
   - Prints per-phase timings and final count to stderr; exit conventions match the
     other mains (usage 2, runtime error 1, 0 on success).
   - Threading via `std::thread` (C++ stdlib; CMake links `Threads::Threads`).
     Decision record: parent spec §8 named OpenMP, but Apple clang ships no
     OpenMP runtime on this machine and installing one was declined — the
     mechanism swaps to std::thread, the experiment (file-grain intra-process
     threading vs multi-process MAS) is unchanged. The atomic-counter pool is
     dynamic load balancing, slightly fairer than PUSH/PULL's static
     round-robin — noted in results.md.
2. **`bench/run_bench.sh`**: the sweep harness (details §5).
3. **`python/bench_plots.py`**: plots + table (details §6).
4. **Docs**: `docs/bench/results.md`, `docs/bench/*.png`, validation-log entry.

## 5. Harness & Metrics

**Matrix**: arch/parallelism {mono T=1, mono T∈{2,4,8}, MAS N∈{1,2,4,8,16}} ×
volume {1 day, 7 days, 28 days} — volume subsets are the first 1/7/28 day-files
in date order (deterministic across runs and architectures). N=16 on 8 cores is the deliberate
oversubscription point (plateau/degradation is part of the story). Repeats R=3,
median reported. ≈81 runs, wall-clock budget ~1.5–2.5 h for the full sweep;
`--quick` runs the 1-day column only.

**Per-run measurements** (one CSV row in `bench/results.csv`):
`arch,n_workers,threads,files,repeat,clean_s,merge_s,total_s,events,rows_per_s,events_per_s,peak_rss_mb,cpu_pct`
- Phases timed separately: cleaning (dispatch→last result for MAS; loop/parallel
  region for mono) and merge; `total_s` includes both.
- `events` read back from the merged store (`mas_merge`-style count); rows/day is
  the known 86,399 constant × files.
- Peak RSS and CPU% via `/usr/bin/time -l` (macOS variant; the plan pins the exact
  field parsing). For MAS, RSS is summed across coordinator+workers, CPU% computed
  from wall vs aggregate user+sys.
- A MAS run = coordinator + N workers + merge, orchestrated like `chaos_e2e.sh`
  without the kill; endpoints on a distinct port block to avoid collisions.

**Correctness while benchmarking**: every run asserts merged count == the
per-volume oracle total (computed once per volume via sequential `clean` runs,
cached). A mismatched run marks the whole sweep FAILED — the benchmark doubles as
a correctness soak of both architectures.

**Fairness rules**: fresh store files per run; fixed file ordering; same tmp
volume; disk guard (abort below 2 GB free before extraction, with a message);
no warm-up discard (median-of-3 absorbs variance); thermal caveat documented in
results.md (laptop, no fan control).

**Volume prep**: idempotent unzip of the month archive (skip files already
extracted, verify 28 files present) — part of the harness, gated by the disk guard.

## 6. Plots & Reporting

`python/bench_plots.py` reads `bench/results.csv` (pandas + matplotlib, both
already used in `python/`):

1. Throughput (events/s) vs N — MAS curve, `mono-1T` horizontal reference,
   `mono-MT(T)` points.
2. Speedup S = T₁/Tₙ and efficiency E = S/N vs N/T, with the ideal-linear line.
3. Wall-clock vs volume (1/7/28 days) per architecture.
4. Monolith threading: speedup vs T.

Outputs: `docs/bench/<name>.png` (committed) + `docs/bench/results.md` with the
median table and a short honest findings section (expected: MAS ~linear until
merge/I-O bound; mono-MT similar until memory-bound; oversubscription plateau;
merge share rising with N).

## 7. Testing

- **Unit/smoke**: `mas_monolith` on 2 tiny fixture CSVs with threads=2 → count
  equals the threads=1 run and the known fixture count; usage/exit-code contract.
- **Real-data correctness gate**: month at T=8 == month at T=1 == oracle total
  (this is the file-parallel correctness proof; the harness then re-asserts it on
  every sweep run).
- **Harness self-checks**: results.csv row count == expected configs × repeats;
  every run's count assertion; disk guard path exercised by a fake low-disk test
  (env override).
- **Regression**: full existing suite stays green; no changes to library code
  expected outside CMake (`find_package(Threads)`) and the new main.

## 8. Risks & Accepted Caveats

| Risk | Handling |
|---|---|
| (retired) OpenMP unavailable | Resolved at design time: std::thread mechanism swap, decision recorded in §4 |
| Laptop thermal variance | Median-of-3; caveat in results.md; no absolute-number claims beyond trends |
| Merge dominates at high N | Timed separately, reported separately — a finding, not a flaw |
| Disk fills mid-sweep | 2 GB pre-flight guard + per-volume extraction check |
| N=16 oversubscription noise | Deliberate; interpreted as plateau evidence |

## 9. Success Criteria

1. `mas_monolith` month count == oracle at T=1 and T=8.
2. Full sweep completes with every run's correctness assertion passing;
   `bench/results.csv` complete.
3. Four plot families + results.md committed; validation-log entry records the
   sweep (hardware, commands, medians).
