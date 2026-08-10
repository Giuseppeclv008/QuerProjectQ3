# CUDA Cleaning Pipeline & Three-Way Efficiency Benchmark — Design Spec

Date: 2026-08-10
Status: approved
Parent spec: `2026-07-04-iiot-data-refinement-mas-design.md` (§3 non-goals, §8)
Sibling spec: `2026-07-10-benchmark-monolith-design.md` (harness, metrics, plot families)
Branch: `feat/cuda-cleaning-bench`

## 1. Context & Problem

The project has two cleaning implementations of the same transform: the graded C++
core (`CapEventExtractor` + `Pipeline`, benchmarked in `bench/results.csv`) and a
pure-Python reference (`python/oracle.py`) used only as a correctness oracle and
never timed. There is no measured statement of what the C++ tier actually buys
over Python, and no third data point to bound how much of the runtime is language
and how much is I/O.

This plan adds a CUDA implementation of the identical transform and runs all three
under one harness on one machine, producing that measurement.

Two hardware facts shape everything below:

- The development machine is an Apple M3. No NVIDIA GPU, no `nvcc`. CUDA code
  written here **cannot be compiled or run here**.
- The benchmark target is a separate PC with an NVIDIA GPU, reachable only by the
  user. The user pulls the branch, runs one command, and pastes the output back.

Consequence: every claim this plan makes must be verifiable **either** on the M3
(algorithm equivalence, Python paths, existing C++ paths) **or** by a single
self-checking script the user runs unattended. No design element may depend on the
author having interactive access to the GPU.

### 1.1 Reversal of a parent-spec non-goal

Parent spec §3 states: "GPU acceleration is **optional stretch** for analytics only,
not the cleaning core." The sibling benchmark spec §2 repeats it: "GPU (§8: optional
stretch, explicitly poor fit for the dedup)."

That judgment rested on the premise that the dedup is a **sequential per-head scan**
with negligible arithmetic intensity — 36 dependent chains, so at most 36-way
parallelism, dominated by loop-carried state. §3 below shows the premise is false:
the transform carries no state beyond one row, making it element-wise over
3,110,364 independent (row, head) pairs per day-file. The non-goal is reversed on
that basis, and only on that basis. If §3's equivalence proof fails at the gate
(§8, T1), this plan is abandoned rather than reworked.

## 2. Goals & Non-Goals

**Goals**

1. A CUDA cleaning binary, `mas_cuda_clean`, that parses raw telemetry CSV **on the
   GPU** and emits a cap-event stream byte-identical to the C++ core's.
2. A vectorized Python cleaner, `python/clean_vectorized.py`, implementing the same
   algorithm with pandas/numpy — the fair Python contender.
3. One self-checking benchmark script that measures **five** implementations on the
   NVIDIA box and asserts correctness on every run.
4. A CPU reference of the element-wise algorithm that runs and is tested on the M3,
   so the load-bearing claim of §3 is proved without a GPU.

**Non-Goals**

- GPU analytics (WP2 tools stay CPU). Parent §3's "analytics only" stretch is
  untouched; this plan does the opposite tier and does not do that one.
- GPU-side DuckDB writes. The store path is unchanged and shared by all arches.
- Multi-GPU, streams overlapping across files, or CUDA graphs. One GPU, one file at
  a time, synchronous stages — the measurement is stage-resolved, so overlap would
  obscure the very breakdown this plan exists to produce.
- Replacing the C++ core. `mas_cuda_clean` is a benchmark contender and an
  alternative path, not the default ingestion route. `scripts/build_store.sh` is
  unchanged.
- Re-running or amending `bench/results.csv`. Those numbers are M3-measured; see §6.4.

## 3. Key Decision — the transform is element-wise, not a scan

This is the load-bearing claim of the whole plan.

`core/src/domain/CapEventExtractor.cpp:22-40`:

```cpp
void CapEventExtractor::process(const RawRow& row, std::vector<CapEvent>& out) {
    for (int h = 0; h < NUM_HEADS; ++h) {
        const long long c = std::llround(row.count[h]);
        auto& last = last_count_[h];
        if (!last.has_value()) { last = c; continue; }   // seed
        if (c > *last)      { out.push_back(makeEvent(row, h, c, c - *last, false)); last = c; }
        else if (c < *last) { out.push_back(makeEvent(row, h, c, 0, true));          last = c; }
        // held (c == *last): emit nothing, `last` already equals c
    }
}
```

**Claim.** After `process` returns for row `i`, `last_count_[h] == llround(count[i][h])`
for every head `h` that has been seeded.

**Proof.** Case analysis on the three branches. The seed branch assigns `last = c`.
The `c > *last` branch assigns `last = c`. The `c < *last` branch assigns `last = c`.
The held branch is entered only when `c == *last`, so `last == c` already holds. In
all four cases the postcondition is `last == c`, and `c` is `llround(count[i][h])`. ∎

**Corollary.** `*last` observed while processing row `i` equals `llround(count[i-1][h])`.
The extractor therefore never reads state older than the immediately preceding row,
and the transform is exactly:

```
for i in 1 .. N-1,  h in 0 .. 35:
    c_prev = llround(count[i-1][h])
    c_cur  = llround(count[i][h])
    if   c_cur >  c_prev:  emit CapEvent(head=h+1, ts=ts[i], cap_seq=c_cur,
                                         torque=torque[i][h], status=status[i][h],
                                         delta=c_cur-c_prev, reset=false)
    elif c_cur <  c_prev:  emit CapEvent(head=h+1, ts=ts[i], cap_seq=c_cur,
                                         torque=torque[i][h], status=status[i][h],
                                         delta=0, reset=true)
    else:                  emit nothing
row 0 emits nothing (seed)
```

Derived fields follow the existing constructor: `is_fault = is_reject(status)`,
`aggregated = delta > 1`.

**Why it matters.** The sequential reading gives 36-way parallelism with a
loop-carried dependence. The element-wise reading gives 86,399 × 36 = **3,110,364
independent threads per day-file**, each doing two loads and one compare. That is
the difference between a GPU port being a stunt and being the fastest tier in the
project.

**Scope of the seed.** `CapEventExtractor` is documented "one instance per
stream/head-partition" and `Pipeline::clean_file` constructs one per file, so the
seed row is row 0 **of each day-file**. The CUDA and vectorized implementations
reproduce that per-file seeding exactly — they are never handed a concatenation of
files.

**Verification, not assertion.** §8 T1 tests the corollary against the shipped
stateful extractor on real day-files. It runs on the M3. If it fails, §1.1 applies.

## 4. Input Format (verified, not assumed)

Confirmed by reading the real pool at
`telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02/`:

| Property | Value |
|---|---|
| Columns | 109 |
| Layout | `timestamp`, `H01..H36 Count`, `H01..H36 AppTorque`, `H01..H36 Status` |
| Grouping | **By field type, not interleaved per head** |
| Field slices | ts `[0]`, count `[1:37]`, torque `[37:73]`, status `[73:109]` |
| Rows | 86,400 (1 header + 86,399 data) |
| Size | ~57.7 MB per day-file |
| Timestamp | `2026-02-21T16:00:00.000` — fixed 23 chars, no quoting |
| Numerics | plain decimal, no quoting, no exponent, no thousands separator |

The AROL brief's slide-4 table shows an interleaved layout; the delivered pool is
grouped. `python/oracle.py` already indexes the grouped layout and is
correctness-locked against the C++ core on real data, which confirms grouped is
correct. The CUDA parser hard-codes the grouped slices and **validates the header
row at startup**, failing loudly on mismatch rather than silently misreading
(§8, T5).

## 5. Components

### 5.1 `CapEventExtractorFlat` (C++, runs on M3)

`core/include/mas/domain/CapEventExtractorFlat.hpp` + `.cpp`.

CPU reference implementation of §3's element-wise form, operating on
column-major arrays rather than a row stream:

```cpp
// count/torque/status: [n_rows][NUM_HEADS], row-major, row 0 is the seed.
// Emits in (row, head) order — the same order CapEventExtractor emits.
void extract_flat(const std::vector<std::string>& ts,
                  const double* count, const double* torque, const double* status,
                  std::size_t n_rows, std::vector<CapEvent>& out);
```

Purpose: it is the executable statement of §3's claim, it is the differential-test
partner for the CUDA kernels, and it is the only piece of the new algorithm that
CI on the M3 can exercise. Emission order is `(row asc, head asc)` — identical to
the stateful extractor's, so outputs compare with `==` and no sorting.

### 5.2 CUDA pipeline (`core/cuda/`, built only when `MAS_ENABLE_CUDA=ON`)

Files: `CudaCleaner.cu`, `CudaCleaner.hpp`, plus `apps/cuda_clean_main.cpp`.

| Stage | Kind | Detail |
|---|---|---|
| S0 read | host | `mmap` the day-file, copy into a pinned (`cudaHostAlloc`) staging buffer |
| S1 upload | H2D | one `cudaMemcpy` of the raw ~57.7 MB byte blob |
| S2 row index | kernel + CUB | flag `'\n'`, `cub::DeviceSelect::Flagged` → `row_offset[]` (86,400 entries) |
| S3 parse | kernel | **one thread per row**; each thread walks its row once, splitting on `','` and parsing 108 numerics inline into `count/torque/status` device arrays; writes the 23-byte ts to a fixed-stride device buffer |
| S4 delta | kernel | **one thread per (row, head)**, `i >= 1`; two loads, one compare; writes `flag[]` and a fully populated `CapEventDevice` slot |
| S5 compact | CUB | `cub::DeviceSelect::Flagged` over `flag[]` → dense event array + count |
| S6 download | D2H | dense events only (~765,711 × 32 B ≈ 24 MB for a typical day-file) |
| S7 store | host | existing `IEventStore`; skipped entirely under `--no-store` |

Design notes:

- **No strings on the GPU beyond a memcpy.** `CapEventDevice` carries a `uint32_t
  row_index`, not a timestamp. The host maps index → timestamp string when
  materializing `CapEvent`s for the store. This removes all device-side string
  handling from the hot path and is why S4's struct is a flat 32 bytes.
- **S3 is thread-per-row, deliberately.** Access is uncoalesced (each thread walks
  ~650 contiguous bytes). The alternative — comma-index pass, then 109 threads per
  row — is more coalesced but needs a 37 MB scratch array and a second kernel. At
  57.7 MB of input the naive version is bandwidth-bound at worst: a 50× efficiency
  loss still lands in single-digit milliseconds against a 3,370 ms host baseline.
  Start simple; §6.3's stage timings will say whether it was ever the bottleneck.
- **S4 writes a full struct per slot before compaction**, trading ~100 MB of
  transient device memory for a single-pass compaction with no gather indirection.
  Peak device footprint per day-file: raw 58 MB + parsed arrays 75 MB + slot array
  100 MB + compacted 24 MB ≈ **260 MB**. Fits any CUDA-capable card from the last
  decade. §9 R4 covers the fallback if it does not.
- **Numeric parse must match `std::stod`.** Counts are integers well under 2^53 and
  are exact. Torque and status are short decimals; the kernel parses them by
  integer-mantissa accumulation followed by a single scale division, which is
  correctly rounded for the digit counts present in this pool. Bitwise equality is
  asserted, not assumed (§8, T3).

### 5.3 `python/clean_vectorized.py` (runs on M3)

```python
def extract(path) -> list[tuple]:   # same 9-tuple shape as oracle.extract
```

`pandas.read_csv` with an explicit 109-name/dtype spec → `numpy.rint` on the 36
count columns → `numpy.diff(axis=0)` → boolean masks for `>0` and `<0` →
`numpy.nonzero` compaction → tuples assembled in `(row, head)` order. No Python-level
row loop. Same per-file seeding as §3.

### 5.4 `bench/run_bench_cuda.sh`

One command, run on the NVIDIA box, self-checking, prints a paste-back table.
Detailed in §6.

## 6. Benchmark

### 6.1 Arches

| Label | Implementation | Notes |
|---|---|---|
| `py-naive` | `python/oracle.py` | pure `csv` module, existing oracle, unmodified |
| `py-numpy` | `python/clean_vectorized.py` | new, §5.3 |
| `mono-1T` | `build/mas_monolith`, T=1 | existing binary, unmodified |
| `mono-MT` | `build/mas_monolith`, T=8 | existing binary, unmodified |
| `cuda` | `build/mas_cuda_clean` | new, §5.2 |

### 6.2 Two timing modes — mandatory

At an estimated ~15 ms of GPU work per day-file, **DuckDB insertion dominates total
runtime by two orders of magnitude** and would compress all five arches into
indistinguishable numbers. Every arch is therefore measured twice:

- **`clean` mode (`--no-store`)** — parse + transform + materialize events in
  memory, no persistence. This is the comparison the plan exists to make.
- **`e2e` mode** — including the DuckDB write. This is the honest deployment number
  and shows how much of the C++ advantage survives contact with the store.

The existing `bench/results.csv` conflates the two; the new sweep separates them.
`mas_monolith` gains a `--no-store` flag; `oracle.py` and `clean_vectorized.py` are
`clean`-mode only by construction and are reported as `n/a` in `e2e`.

### 6.3 Outputs

`bench/results_cuda.csv` — the existing 13-column schema plus `mode`:

```
arch,mode,n_workers,threads,files,repeat,clean_s,merge_s,total_s,events,rows_per_s,events_per_s,peak_rss_mb,cpu_pct
```

`bench/results_cuda_stages.csv` — CUDA only, from `cudaEvent` timers:

```
files,repeat,read_s,h2d_s,s2_index_s,s3_parse_s,s4_delta_s,s5_compact_s,d2h_s,store_s
```

The stage table is the technically interesting artifact: it says exactly how much of
the win is "GPU parses CSV" versus "GPU does the compare", which is the question
§1.1 turns on.

### 6.4 Volumes, repeats, and the `py-naive` cap

Volumes 1 / 7 / 28 day-files, 3 repeats, matching the sibling spec's sweep so the
shapes are comparable.

**Exception:** `py-naive` processes ~25k rows/s at best; 28 day-files is ~40 minutes
per repeat, ~2 hours for the arch. It is therefore run at **the 1-day volume only**,
3 repeats, and its 7/28-day figures are reported as a linear extrapolation
explicitly labelled `extrapolated` in the results table and in `validation-log.md`.
The transform is O(rows) with no cross-file state, so linear extrapolation is sound
for `clean` mode; it is not claimed for `e2e`.

**Cross-machine hygiene.** `bench/results.csv` was measured on the M3 and is not
touched, not appended to, and never compared against the new file. The NVIDIA box
re-measures `mono-1T`, `mono-MT`, `py-naive`, and `py-numpy` itself, so every number
in `results_cuda.csv` comes from one machine. The script records
`uname -a`, `nvidia-smi --query-gpu=name,memory.total,driver_version`, CPU model, and
`nvcc --version` into a header comment block so the provenance travels with the data.

### 6.5 Correctness on every run

Each run's event count is checked against `python/oracle_union.py` for that volume,
exactly as `bench/run_bench.sh` does today. A mismatch aborts the sweep with a
non-zero exit — a fast implementation that is wrong must never produce a row.

### 6.6 Plots

`python/bench_plots.py` gains a `--cuda` mode emitting into `docs/bench/`:

1. Throughput bar chart, five arches × two modes, 1-day volume.
2. `clean_s` versus volume, log-y, all five arches.
3. CUDA stage breakdown, stacked bars, per volume.

## 7. Build & Repository Layout

```
core/
  include/mas/domain/CapEventExtractorFlat.hpp   (new)
  src/domain/CapEventExtractorFlat.cpp           (new)
  cuda/CudaCleaner.hpp                           (new)
  cuda/CudaCleaner.cu                            (new)
  src/apps/cuda_clean_main.cpp                   (new)
  src/apps/monolith_main.cpp                     (modified: --no-store)
python/
  clean_vectorized.py                            (new)
  tests/test_clean_vectorized.py                 (new)
  bench_plots.py                                 (modified: --cuda)
tests/
  test_cap_event_extractor_flat.cpp              (new)
bench/
  run_bench_cuda.sh                              (new)
docs/bench/                                      (new plots)
```

CMake:

```cmake
option(MAS_ENABLE_CUDA "Build the CUDA cleaning pipeline" OFF)
if(MAS_ENABLE_CUDA)
    find_package(CUDAToolkit REQUIRED)
    enable_language(CUDA)
    add_executable(mas_cuda_clean core/cuda/CudaCleaner.cu core/src/apps/cuda_clean_main.cpp)
    target_link_libraries(mas_cuda_clean PRIVATE mas_core CUDA::cudart)
    set_target_properties(mas_cuda_clean PROPERTIES CUDA_ARCHITECTURES native)
endif()
```

CUB ships inside the CUDA Toolkit; no new third-party dependency. `CUDA_ARCHITECTURES
native` means the user's box compiles for its own card with no configuration.

**Default-OFF is a hard requirement.** With `MAS_ENABLE_CUDA=OFF` — the M3's
configuration — no CUDA language is enabled, no `.cu` file is compiled, and the
existing 73 C++ and 219 Python tests must remain green and unchanged in count except
for the new tests this plan adds.

## 8. Testing

| # | Test | Runs on | Asserts |
|---|---|---|---|
| T1 | `test_cap_event_extractor_flat.cpp` — differential vs `CapEventExtractor` | **M3** | Identical `CapEvent` vectors (all 9 fields) on synthetic edge cases *and* on a real day-file: seeding, held runs, `delta > 1`, counter reset, reset-then-advance, all-36-heads-simultaneous, single-row file, empty file. **This is §3's proof gate.** |
| T2 | `test_clean_vectorized.py` — differential vs `oracle.py` | **M3** | Identical tuple lists on the `tiny_store` fixtures and one real day-file |
| T3 | CUDA vs `CapEventExtractorFlat`, invoked by the bench script | NVIDIA box | Identical event streams; torque/status compared **bitwise** (`memcmp` on the doubles), not with a tolerance |
| T4 | `mas_cuda_clean` vs `oracle_union.py` row count, per sweep run | NVIDIA box | Exact equality; mismatch aborts |
| T5 | Header validation | both | `mas_cuda_clean` and `clean_vectorized` reject a file whose header is not the §4 layout, with a message naming the offending column |
| T6 | `--no-store` parity | **M3** | `mas_monolith --no-store` reports the same event count as a normal run on the same file |

T1 is the one that decides whether this plan proceeds. It requires no GPU, so it is
written and run **first**, before any `.cu` file exists.

## 9. Risks & Accepted Caveats

| # | Risk | Mitigation |
|---|---|---|
| R1 | §3's equivalence claim is wrong | T1 catches it on the M3 before any CUDA work starts. If it fails, the plan is abandoned per §1.1 — not patched with a scan-based kernel. |
| R2 | GPU float parse differs from `std::stod` in the last ulp | T3 compares bitwise on real data. If it fails, S3 switches to exact integer-mantissa accumulation with a lookup-table scale; the pool's decimals are ≤3 places, so an exact path always exists. |
| R3 | Author cannot run or debug anything CUDA | The bench script is self-checking and prints full diagnostics on failure (stage timings, first 10 mismatching events with expected/actual). A failed run produces a paste-back artifact sufficient to debug offline. |
| R4 | Device memory insufficient for the 260 MB working set | Only on cards below ~1 GB. The binary queries `cudaMemGetInfo` at startup and, if short, falls back to processing the file in row-chunks of 16,384 rows with the previous chunk's last row carried forward as the seed — a correctness-neutral change under §3, since the dependence is one row deep. |
| R5 | `py-naive` extrapolation is challenged as unrigorous | Labelled `extrapolated` in every artifact, applied only to `clean` mode, and justified by the transform's O(rows), no-cross-file-state property. The 1-day measurement is real. |
| R6 | Results tempt a "CUDA replaces the C++ core" conclusion | §2 non-goals state it does not, and `e2e` mode is reported alongside `clean` precisely so the store cost is visible. The write-up states the deployment conclusion explicitly. |
| R7 | Reviewer reads this as contradicting parent §3 | §1.1 states the reversal, its single technical basis, and the gate that would undo it. |

## 10. Success Criteria

1. T1 passes on the M3 — §3's claim is proved against the shipped extractor on real data.
2. `MAS_ENABLE_CUDA=OFF` build on the M3 is green: 73 C++ tests + 219 Python tests still pass, plus the new T1/T2/T6.
3. `mas_cuda_clean` produces event streams byte-identical to `CapEventExtractorFlat` and row counts equal to `oracle_union.py` at all three volumes.
4. `bench/run_bench_cuda.sh` completes unattended on the NVIDIA box in one command and emits both CSVs with machine provenance recorded.
5. Five arches × two modes are measured on one machine, with the CUDA stage breakdown resolved.
6. `docs/validation-log.md` gains an entry with the numbers, the extrapolation caveat, and a plain statement of which tier is fastest for `clean` and which for `e2e`.
