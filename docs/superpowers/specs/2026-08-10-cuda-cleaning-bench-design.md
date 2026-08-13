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

This plan adds a CUDA implementation of the identical transform and runs all
contenders under one harness on one machine, producing that measurement.

Three hardware and platform facts shape everything below:

- The development machine is an Apple M3 running macOS. No NVIDIA GPU, no `nvcc`.
  CUDA code written here **cannot be compiled or run here**.
- The benchmark target is a **Windows PC with an NVIDIA GPU**, reachable only by
  the user. The user pulls the branch, runs two commands, and pastes the output back.
- The deliverable must be **runnable independently on any PC** — Windows, Linux, or
  macOS — without the author present and without hand-editing anything.

Consequence: every claim this plan makes must be verifiable **either** on the M3
(algorithm equivalence, Python paths, existing C++ paths, the portable build itself)
**or** by a single self-checking script the user runs unattended. No design element
may depend on the author having interactive access to the GPU or to Windows.

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
(§9, T1), this plan is abandoned rather than reworked.

### 1.2 The ZeroMQ layer is kept, not removed

Reviewed and decided during design: the ZMQ/agent layer is 1,588 LOC carrying
**40 of 73 C++ tests** and **45 of 81 benchmark rows**. It is not required by the
AROL brief — nothing in those 20 slides asks for IPC, queues, or multi-process work —
but it is the systems-programming content that makes this an SDP project, and
`mas` vs `mono-MT` is precisely the "processes + IPC vs threads + shared memory"
axis that the scalability proof rests on. Adding CUDA extends that axis to three
points rather than replacing it.

It is therefore **kept on `main` and compiled out of the portable build** via
`MAS_ENABLE_ZMQ=OFF` (§7). The CUDA pipeline has no dependency on it. Building
libzmq from source under MSVC is slow and fiddly for zero benefit to this
measurement, which is the only problem the flag solves.

## 2. Goals & Non-Goals

**Goals**

1. A CUDA cleaning binary, `mas_cuda_clean`, that parses raw telemetry CSV **on the
   GPU** and emits a cap-event stream byte-identical to the C++ core's.
2. A vectorized Python cleaner, `python/clean_vectorized.py`, implementing the same
   algorithm with pandas/numpy — the fair Python contender.
3. **A dependency-free portable build** (`MAS_BENCH_ONLY=ON`) that compiles and runs
   the full benchmark on Windows, Linux, or macOS with nothing but CMake, a C++20
   compiler, Python, and — for the CUDA target — the CUDA Toolkit.
4. One self-checking benchmark driver that measures every contender on the target
   machine and asserts correctness on every run.
5. A CPU reference of the element-wise algorithm that runs and is tested on the M3,
   so the load-bearing claim of §3 is proved without a GPU.

**Non-Goals**

- GPU analytics (WP2 tools stay CPU). Parent §3's "analytics only" stretch is
  untouched; this plan does the opposite tier and does not do that one.
- GPU-side DuckDB writes. The store path is unchanged and shared by all arches.
- Multi-GPU, streams overlapping across files, or CUDA graphs. One GPU, one file at
  a time, synchronous stages — the measurement is stage-resolved, so overlap would
  obscure the very breakdown this plan exists to produce.
- Replacing the C++ core, or removing the ZMQ layer (§1.2). `mas_cuda_clean` is a
  benchmark contender and an alternative path, not the default ingestion route.
  `scripts/build_store.sh` is unchanged.
- Porting the ZeroMQ agent runtime to Windows. `MAS_ENABLE_ZMQ` is `OFF` there;
  the MAS arch is not part of the CUDA comparison.
- Re-running or amending `bench/results.csv`. Those numbers are M3-measured; see §6.5.

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

**Verification, not assertion.** §9 T1 tests the corollary against the shipped
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
| Line endings | **CRLF** — every row, header included (86,400 `\r` against 86,400 `\n` per day-file, and the zip members carry the `\r` themselves; the original "LF, verified with `od -c`" entry was wrong) |

> **Correction (2026-08-10, first run on the target box).** "Plain decimal"
> hid a wrong implicit premise: ~2% of AppTorque cells (66,553 on day 1; none
> in Count or Status) carry the **full 17-significant-digit repr of a double**,
> e.g. `2.0020000000000002`. A 17-digit mantissa exceeds 2^53, so the CUDA
> parser's integer-mantissa-then-divide path double-rounds exactly there —
> caught bitwise by `--verify` at event 25,194 of day 1. The kernel now flags
> such cells per row and the host re-parses the flagged events' payloads with
> `strtod` (see `CudaCleaner.cu`); a >2^53 mantissa in a *Count* cell is a hard
> error, and none exists in the pool.

The AROL brief's slide-4 table shows an interleaved layout; the delivered pool is
grouped. `python/oracle.py` already indexes the grouped layout and is
correctness-locked against the C++ core on real data, which confirms grouped is
correct. The CUDA parser hard-codes the grouped slices and **validates the header
row at startup**, failing loudly on mismatch rather than silently misreading
(§9, T5).

**CRLF handling.**

> **Correction (2026-08-13, review).** This paragraph originally opened with
> "The pool is LF, so the data path is safe" and treated CRLF as a
> hypothetical introduced by `core.autocrlf`. The premise is inverted: the
> pool is CRLF on every row, inside the zips, so CRLF is the normal condition
> of the data path, not a risk to it.

The trailing-`\r` strip in the CUDA row-index kernel and in
`CapEventExtractorFlat` is therefore load-bearing on 100% of the pool, not a
defensive extra. `CsvRawReader`, the production reader, strips the `\r` only
on the header line (`splitCsv`); on data rows it relies on `std::stod("0.0\r")`
parsing and stopping at the `\r` — which works, on every row of every file it
has ever read, but by the parser's stopping rule rather than by design. The
`.gitattributes` pinning `*.csv` fixtures still matters for the *committed*
fixtures, which are LF and must stay byte-stable across a Windows clone; the
CRLF-equivalence tests (T8) are what tie the two shapes together.

## 5. Components

### 5.1 Target split — the precondition for portability

Today `mas_core` bundles the cleaning hot path with the DuckDB stores, so anything
that cleans also links `duckdb_imported`. Split it:

| Target | Sources | Dependencies |
|---|---|---|
| **`mas_clean_core`** | `CapEventExtractor.cpp`, `CapEventExtractorFlat.cpp`, `CsvRawReader.cpp` | **none** — C++20 stdlib only |
| `mas_store` | `CsvEventStore.cpp`, `DuckDbEventStore.cpp`, `Pipeline.cpp` | `mas_clean_core`, `duckdb_imported` |
| `mas_agent` | `Message.cpp`, `CleaningWorker.cpp`, `Coordinator.cpp` | `mas_store` |
| `mas_transport` | `ZmqTransport.cpp` | `cppzmq` |

Verified precondition: `core/` contains **zero** POSIX-only includes — no `unistd.h`,
`sys/mman.h`, `mmap`, `getrusage`, `pthread`, `fork`, or `dirent.h`. The cleaning
code is already portable; only the build system and the shell scripts are not.

`mas_clean_core` is what makes `MAS_BENCH_ONLY` (§7) possible: it downloads nothing
and links nothing.

### 5.2 `CapEventExtractorFlat` (C++, runs on M3)

`core/include/mas/domain/CapEventExtractorFlat.hpp` + `core/src/domain/CapEventExtractorFlat.cpp`.

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
partner for the CUDA kernels, and it is the only piece of the new algorithm that the
M3 can exercise. Emission order is `(row asc, head asc)` — identical to the stateful
extractor's, so outputs compare with `==` and no sorting.

### 5.3 `platform_metrics.hpp` (header-only, ~40 lines)

`core/include/mas/util/platform_metrics.hpp`.

```cpp
struct ProcMetrics { double wall_s, cpu_s; double peak_rss_mb; };
ProcMetrics read_metrics();     // since process start
```

Windows: `GetProcessMemoryInfo` (`psapi.h`) + `GetProcessTimes`.
POSIX: `getrusage(RUSAGE_SELF)`.

This replaces the external timing wrapper. `bench/run_bench.sh` currently shells out
to `/usr/bin/time -l`, which is BSD-specific — the flag does not exist on GNU
coreutils and there is no equivalent on Windows at all. Having each binary report its
own numbers on a parseable stdout line is portable, and more accurate, since it
excludes process spawn.

### 5.4 `bench_cpu` — the portable C++ contender

`core/src/apps/bench_cpu_main.cpp`, links **only** `mas_clean_core` + `Threads::Threads`.

Mirrors `monolith_main.cpp`'s file-grain threading — a fixed pool of `T` `std::thread`s
pulling day-files off an atomic counter — but accumulates events in memory instead of
writing a store. Same `CsvRawReader` → `CapEventExtractor` hot path, byte for byte.

This exists because requiring DuckDB on Windows just to get a C++ number would make
the headline measurement hostage to the store dependency. `mas_monolith` remains the
graded binary and still appears in the sweep whenever the full build is available
(§6.2); T7 asserts the two agree on event counts so the substitution is auditable
rather than assumed.

### 5.5 CUDA pipeline (`core/cuda/`, built only when `MAS_ENABLE_CUDA=ON`)

Files: `core/cuda/CudaCleaner.cu`, `core/cuda/CudaCleaner.hpp`, `core/src/apps/cuda_clean_main.cpp`.

| Stage | Kind | Detail |
|---|---|---|
| S0 read | host | `cudaHostAlloc` pinned buffer, filled by `std::ifstream::read` in binary mode |
| S1 upload | H2D | one `cudaMemcpy` of the raw ~57.7 MB byte blob |
| S2 row index | kernel + CUB | flag `'\n'`, `cub::DeviceSelect::Flagged` → `row_offset[]` (86,400 entries); trailing `'\r'` trimmed per row |
| S3 parse | kernel | **one thread per row**; each thread walks its row once, splitting on `','` and parsing 108 numerics inline into `count/torque/status` device arrays; writes the 23-byte ts to a fixed-stride device buffer |
| S4 delta | kernel | **one thread per (row, head)**, `i >= 1`; two loads, one compare; writes `flag[]` and a fully populated `CapEventDevice` slot |
| S5 compact | CUB | `cub::DeviceSelect::Flagged` over `flag[]` → dense event array + count |
| S6 download | D2H | dense events only (~765,711 × 32 B ≈ 24 MB for a typical day-file) |
| S7 store | host | existing `IEventStore`; **compiled out** under `MAS_BENCH_ONLY`, skipped under `--no-store` |

Design notes:

- **No `mmap`.** An earlier draft specified it; it is POSIX-only and would have needed
  a `CreateFileMapping` branch for Windows. A plain binary `ifstream::read` into the
  pinned buffer costs the same at 58 MB and is one code path everywhere.
- **No strings on the GPU beyond a memcpy.** `CapEventDevice` carries a `uint32_t
  row_index`, not a timestamp. The host maps index → timestamp string when
  materializing `CapEvent`s. This removes all device-side string handling from the
  hot path and is why S4's struct is a flat 32 bytes.
- **S3 is thread-per-row, deliberately.** Access is uncoalesced (each thread walks
  ~650 contiguous bytes). The alternative — comma-index pass, then 109 threads per
  row — is more coalesced but needs a 37 MB scratch array and a second kernel. At
  57.7 MB of input the naive version is bandwidth-bound at worst: a 50× efficiency
  loss still lands in single-digit milliseconds against a 3,370 ms host baseline.
  Start simple; §6.4's stage timings will say whether it was ever the bottleneck.
- **S4 writes a full struct per slot before compaction**, trading ~100 MB of
  transient device memory for a single-pass compaction with no gather indirection.
  Peak device footprint per day-file: raw 58 MB + parsed arrays 75 MB + slot array
  100 MB + compacted 24 MB ≈ **260 MB**. Fits any CUDA-capable card from the last
  decade. §10 R4 covers the fallback if it does not.
- **Numeric parse must match `std::stod`.** Counts are integers well under 2^53 and
  are exact. Torque and status are short decimals; the kernel parses them by
  integer-mantissa accumulation followed by a single scale division, which is
  correctly rounded for the digit counts present in this pool. Bitwise equality is
  asserted, not assumed (§9, T3).

### 5.6 `python/clean_vectorized.py` (runs on M3)

```python
def extract(path) -> list[tuple]:   # same 9-tuple shape as oracle.extract
```

`pandas.read_csv` with an explicit 109-name/dtype spec → `numpy.rint` on the 36
count columns → `numpy.diff(axis=0)` → boolean masks for `>0` and `<0` →
`numpy.nonzero` compaction → tuples assembled in `(row, head)` order. No Python-level
row loop. Same per-file seeding as §3.

### 5.7 `bench/run_bench_cuda.py` — the driver

Pure Python 3.9+, standard library only except for what the contenders themselves
need (`pandas`, `numpy`). Replaces bash entirely.

`bench/run_bench.sh` cannot run on Windows: it needs bash, `/usr/bin/time -l`,
`unzip`, `find`, `sed`, `awk`, and it works around macOS bash 3.2's lack of
associative arrays. Python is already a hard requirement here — it *is* two of the
contenders — so the driver costs no new dependency and removes six.

Responsibilities:

- Extract day-files from the month zip with `zipfile` (idempotent, skips existing).
- Free-disk guard via `shutil.disk_usage`, mirroring the existing script's floors.
- Discover binaries under `build/` and `build/Release/` (MSVC multi-config puts them
  in the latter).
- Run each arch × mode × volume × repeat, parse the self-reported metrics line (§5.3).
- Check every run's event count against `python/oracle_union.py`; abort non-zero on
  mismatch.
- Emit both CSVs (§6.4) with a machine-provenance header.
- Print a paste-back summary table.

## 6. Benchmark

### 6.1 Two timing modes — mandatory

At an estimated ~15 ms of GPU work per day-file, **DuckDB insertion dominates total
runtime by two orders of magnitude** and would compress every arch into
indistinguishable numbers. Contenders are therefore measured in two modes:

- **`clean`** — parse + transform + materialize events in memory, no persistence.
  This is the comparison the plan exists to make, and it is available in every build
  on every platform.
- **`e2e`** — including the DuckDB write. The honest deployment number, showing how
  much of the C++ advantage survives contact with the store. Requires the full build.

The existing `bench/results.csv` conflates the two; the new sweep separates them.

### 6.2 Arches

| Label | Implementation | Mode | Build |
|---|---|---|---|
| `py-naive` | `python/oracle.py`, unmodified | `clean` | any |
| `py-numpy` | `python/clean_vectorized.py` (§5.6) | `clean` | any |
| `cpp-1T` | `bench_cpu` T=1 (§5.4) | `clean` | portable + full |
| `cpp-MT` | `bench_cpu` T=8 | `clean` | portable + full |
| `cuda` | `mas_cuda_clean` | `clean` + `e2e` | needs `MAS_ENABLE_CUDA=ON` |
| `mono-1T` | `mas_monolith --no-store` / normal | `clean` + `e2e` | full only |
| `mono-MT` | `mas_monolith` T=8 | `clean` + `e2e` | full only |

`mas_monolith` gains a `--no-store` flag so it can be compared in `clean` mode where
the full build exists. `mas` (the ZMQ arch) is **not** in this sweep — §1.2; its
scalability numbers live in `bench/results.csv` and are unaffected.

On the Windows target the expected row set is `py-naive`, `py-numpy`, `cpp-1T`,
`cpp-MT`, `cuda` in `clean` mode. If the user also configures the full build, the
`mono-*` and `e2e` rows appear automatically; the driver skips silently on missing
binaries and records which arches ran.

### 6.3 Volumes, repeats, and the `py-naive` cap

Volumes 1 / 7 / 28 day-files, 3 repeats, matching the sibling spec's sweep so the
shapes are comparable.

**Exception:** `py-naive` processes ~25k rows/s at best; 28 day-files is ~40 minutes
per repeat, ~2 hours for the arch. It is therefore run at **the 1-day volume only**,
3 repeats, and its 7/28-day figures are reported as a linear extrapolation
explicitly labelled `extrapolated` in the results table and in `validation-log.md`.
The transform is O(rows) with no cross-file state, so linear extrapolation is sound
for `clean` mode; it is not claimed for `e2e`.

### 6.4 Outputs

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

### 6.5 Provenance and cross-machine hygiene

`bench/results.csv` was measured on the M3 and is not touched, not appended to, and
never compared against the new file. The target machine re-measures every contender
itself, so every number in `results_cuda.csv` comes from one machine.

The driver records into a header comment block: OS and version (`platform.platform()`),
CPU model, core count, total RAM, Python version, compiler ID and version (from
CMake's cache), `nvcc --version`, and
`nvidia-smi --query-gpu=name,memory.total,driver_version`. Provenance travels with
the data.

### 6.6 Plots

`python/bench_plots.py` gains a `--cuda` mode emitting into `docs/bench/`:

1. Throughput bar chart, all available arches × modes, 1-day volume.
2. `clean_s` versus volume, log-y, all arches.
3. CUDA stage breakdown, stacked bars, per volume.

## 7. Build & Portability

### 7.1 CMake options

| Option | Default | Effect |
|---|---|---|
| `MAS_BENCH_ONLY` | `OFF` | Build only `mas_clean_core`, `bench_cpu`, optionally `mas_cuda_clean`, and the tests that need no store. **Downloads nothing except GoogleTest.** |
| `MAS_ENABLE_ZMQ` | `ON` | `OFF` drops `mas_transport`, `mas_worker`, `mas_coordinator`, and their 4 test files. Forced `OFF` by `MAS_BENCH_ONLY`. |
| `MAS_ENABLE_CUDA` | `OFF` | Enables the CUDA language and builds `mas_cuda_clean`. |
| `MAS_BUILD_TESTS` | `ON` | `OFF` skips GoogleTest entirely — the only remaining download under `MAS_BENCH_ONLY`. |

One honesty note on "dependency-free": `MAS_BENCH_ONLY=ON` still fetches GoogleTest
on first configure, so the *first* build needs network. `-DMAS_BUILD_TESTS=OFF`
(new, default `ON`) skips even that and makes the benchmark build fully offline.
No DuckDB, no libzmq, no CUB download in any case — CUB ships with the Toolkit.

`MAS_BENCH_ONLY=OFF, MAS_ENABLE_ZMQ=ON, MAS_ENABLE_CUDA=OFF` — the default triple —
reproduces today's build exactly. **All 73 existing C++ tests and 219 Python tests
must still pass unchanged**, except for the new tests this plan adds.

### 7.2 Windows support in the full build

Needed only for `e2e` mode; the headline measurement does not depend on it.

- DuckDB asset branch: `libduckdb-windows-amd64.zip`, `IMPORTED_LOCATION` →
  `duckdb.dll`, plus **`IMPORTED_IMPLIB` → `duckdb.lib`**, which the current
  `APPLE`/`else()` branches do not set because ELF and Mach-O do not need one.
- `BUILD_RPATH` is a no-op on Windows. Replaced for all platforms by a post-build
  `copy_if_different` of `$<TARGET_RUNTIME_DLLS:...>` next to each executable.
- The `else()` branch currently hard-codes the Linux asset with an empty SHA256. It
  becomes an explicit three-way `WIN32 / APPLE / UNIX` selection; the two unpinned
  hashes are filled in on first verified download and the empty-hash fallback is kept
  until then, as today.

### 7.3 CUDA configuration

```cmake
if(MAS_ENABLE_CUDA)
    find_package(CUDAToolkit REQUIRED)
    enable_language(CUDA)
    add_executable(mas_cuda_clean core/cuda/CudaCleaner.cu core/src/apps/cuda_clean_main.cpp)
    target_link_libraries(mas_cuda_clean PRIVATE mas_clean_core CUDA::cudart)
    if(CMAKE_VERSION VERSION_GREATER_EQUAL 3.24)
        set_target_properties(mas_cuda_clean PROPERTIES CUDA_ARCHITECTURES native)
    else()
        set_target_properties(mas_cuda_clean PROPERTIES CUDA_ARCHITECTURES "60;70;75;80;86;89")
    endif()
endif()
```

CUB ships inside the CUDA Toolkit; no new third-party dependency. `native` requires
CMake ≥ 3.24 and the target machine's version is unknown, hence the explicit fallback
list. `project()` must gain `CUDA` conditionally rather than unconditionally, or
non-CUDA machines fail at configure time.

On Windows, `nvcc` uses MSVC `cl.exe` as its host compiler — the supported and
expected configuration. No MinGW path is offered or tested.

### 7.4 What the user runs

Prerequisites on Windows: **Visual Studio 2022 Build Tools, CUDA Toolkit, CMake,
Python 3.9+**. Nothing else.

```
cmake -S . -B build -DMAS_BENCH_ONLY=ON -DMAS_ENABLE_CUDA=ON
cmake --build build --config Release
pip install -r bench/requirements-bench.txt
python bench/run_bench_cuda.py --data telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02.zip
```

Identical on Linux and macOS apart from `--config Release` being redundant on
single-config generators, which is harmless. `bench/README.md` documents this,
including how to add the full build for `e2e` rows.

### 7.5 Repository layout

```
core/
  include/mas/domain/CapEventExtractorFlat.hpp   (new)
  include/mas/util/platform_metrics.hpp          (new)
  src/domain/CapEventExtractorFlat.cpp           (new)
  cuda/CudaCleaner.hpp                           (new)
  cuda/CudaCleaner.cu                            (new)
  src/apps/bench_cpu_main.cpp                    (new)
  src/apps/cuda_clean_main.cpp                   (new)
  src/apps/monolith_main.cpp                     (modified: --no-store)
python/
  clean_vectorized.py                            (new)
  tests/test_clean_vectorized.py                 (new)
  bench_plots.py                                 (modified: --cuda)
tests/
  test_cap_event_extractor_flat.cpp              (new)
bench/
  run_bench_cuda.py                              (new)
  requirements-bench.txt                         (new)
  README.md                                      (new)
docs/bench/                                      (new plots)
.gitattributes                                   (new)
CMakeLists.txt                                   (modified: §7.1-7.3, target split §5.1)
```

## 8. Error Handling

The driver runs unattended on a machine the author cannot reach, so every failure
must be diagnosable from one paste.

- **Missing prerequisite** (no `nvcc`, no pandas, no binaries) — named explicitly with
  the command that installs or builds it. Never a stack trace.
- **Header mismatch** (§4) — the offending column index, its expected name, and its
  actual name.
- **Correctness mismatch** (T3/T4) — expected vs actual event count, plus the **first
  10 differing events with all 9 fields side by side**. This is the artifact that lets
  a parse bug be fixed offline without GPU access.
- **CUDA runtime error** — `cudaGetErrorString` plus the stage that raised it.
- **Insufficient device memory** — reported with the chunked fallback (R4) engaging
  automatically, not an abort.
- **Insufficient disk** — checked before extraction, with the shortfall in MB.

The driver exits non-zero on any correctness failure and writes no CSV row for a
failed run. A fast implementation that is wrong must never produce a number.

## 9. Testing

| # | Test | Runs on | Asserts |
|---|---|---|---|
| T1 | `test_cap_event_extractor_flat.cpp` — differential vs `CapEventExtractor` | **M3** | Identical `CapEvent` vectors (all 9 fields) on synthetic edge cases *and* on a real day-file: seeding, held runs, `delta > 1`, counter reset, reset-then-advance, all-36-heads-simultaneous, single-row file, empty file. **This is §3's proof gate.** |
| T2 | `test_clean_vectorized.py` — differential vs `oracle.py` | **M3** | Identical tuple lists on the `tiny_store` fixtures and one real day-file |
| T3 | CUDA vs `CapEventExtractorFlat`, invoked by the driver | target box | Identical event streams; torque/status compared **bitwise** (`memcmp` on the doubles), not with a tolerance |
| T4 | `mas_cuda_clean` vs `oracle_union.py` row count, per sweep run | target box | Exact equality; mismatch aborts |
| T5 | Header validation | both | `mas_cuda_clean` and `clean_vectorized` reject a file whose header is not the §4 layout, naming the offending column |
| T6 | `--no-store` parity | **M3** | `mas_monolith --no-store` reports the same event count as a normal run on the same file |
| T7 | `bench_cpu` vs `mas_monolith` parity | **M3** | Same event count and same event stream on the same file — makes §5.4's substitution auditable |
| T8 | CRLF tolerance | **M3** | A fixture rewritten with CRLF yields byte-identical events through `CapEventExtractorFlat` and `clean_vectorized` |
| T9 | **Portable-build smoke** | **M3** | A clean `-DMAS_BENCH_ONLY=ON` configure+build in an empty directory succeeds, produces `bench_cpu`, and downloads no DuckDB and no libzmq — proving §2 goal 3 without Windows |

T1 is the one that decides whether this plan proceeds. It requires no GPU, so it is
written and run **first**, before any `.cu` file exists.

T9 is the one that proves portability locally. macOS with `MAS_BENCH_ONLY=ON` exercises
the same CMake path Windows will take, minus the MSVC-specific and CUDA-specific
branches, which remain unverifiable here and are called out in R3.

## 10. Risks & Accepted Caveats

| # | Risk | Mitigation |
|---|---|---|
| R1 | §3's equivalence claim is wrong | T1 catches it on the M3 before any CUDA work starts. If it fails, the plan is abandoned per §1.1 — not patched with a scan-based kernel. |
| R2 | GPU float parse differs from `std::stod` in the last ulp | T3 compares bitwise on real data. If it fails, S3 switches to exact integer-mantissa accumulation with a lookup-table scale; the pool's decimals are ≤3 places, so an exact path always exists. |
| R3 | **MSVC and CUDA compile paths cannot be tested by the author.** T9 covers the CMake logic on Clang/macOS, not `cl.exe`, not `nvcc`. | Kept minimal and conventional: no MSVC-specific pragmas, no compiler intrinsics, C++20 features limited to what MSVC 19.3x ships. `platform_metrics.hpp` is the only `#ifdef _WIN32` in the codebase. First Windows configure failure is expected to be a one-line fix, and §8's diagnostics are designed to make it a single round-trip. |
| R4 | Device memory insufficient for the 260 MB working set | Only on cards below ~1 GB. The binary queries `cudaMemGetInfo` at startup and, if short, falls back to processing the file in row-chunks of 16,384 rows with the previous chunk's last row carried forward as the seed — correctness-neutral under §3, since the dependence is one row deep. |
| R5 | `py-naive` extrapolation is challenged as unrigorous | Labelled `extrapolated` in every artifact, applied only to `clean` mode, and justified by the transform's O(rows), no-cross-file-state property. The 1-day measurement is real. |
| R6 | Results tempt a "CUDA replaces the C++ core" conclusion | §2 non-goals state it does not, and `e2e` mode is reported alongside `clean` precisely so the store cost is visible. The write-up states the deployment conclusion explicitly. |
| R7 | Reviewer reads this as contradicting parent §3 | §1.1 states the reversal, its single technical basis, and the gate that would undo it. |
| R8 | The target split (§5.1) breaks the existing build | Mechanical change, no source edits beyond includes; the default option triple must leave all 73 C++ and 219 Python tests green. That is success criterion 2, checked before any CUDA work. |
| R9 | Windows DuckDB asset misbehaves, blocking `e2e` | `MAS_BENCH_ONLY` is the guaranteed path and needs no DuckDB. `e2e` rows are a bonus; their absence does not affect the headline comparison. |

## 11. Success Criteria

1. T1 passes on the M3 — §3's claim is proved against the shipped extractor on real data.
2. The default build on the M3 is green: 73 C++ tests + 219 Python tests still pass, unchanged, plus the new M3-runnable tests (T1, T2, T6, T7, T8).
3. T9 passes: `-DMAS_BENCH_ONLY=ON` builds from empty with no DuckDB and no libzmq download.
4. `mas_cuda_clean` produces event streams byte-identical to `CapEventExtractorFlat` and row counts equal to `oracle_union.py` at all three volumes.
5. `python bench/run_bench_cuda.py` completes unattended on the Windows box after the two documented CMake commands, and emits both CSVs with machine provenance recorded.
6. Every available contender is measured on that one machine, with the CUDA stage breakdown resolved.
7. `docs/validation-log.md` gains an entry with the numbers, the extrapolation caveat, and a plain statement of which tier is fastest for `clean` and which for `e2e`.
