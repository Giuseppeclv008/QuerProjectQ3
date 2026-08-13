# CUDA Cleaning Pipeline & Portable Three-Way Benchmark — Implementation Plan

> **Status: executed.** Every phase below is implemented and committed on
> `feat/cuda-cleaning-bench`; the sweep ran on the Windows RTX box on
> 2026-08-10 (see `docs/validation-log.md`). The boxes were never ticked
> during execution, so an unchecked box here means "not tracked", not "not
> done".

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a CUDA implementation of the cap-event cleaning transform and a dependency-free benchmark that measures Python, C++, and CUDA on one machine — buildable and runnable unattended on Windows, Linux, or macOS.

**Architecture:** `CapEventExtractor` is proved to be element-wise over consecutive row pairs (spec §3), so the transform becomes 3.1M independent GPU threads per day-file instead of 36 dependent chains. `mas_core` splits so the cleaning hot path links nothing; a `MAS_BENCH_ONLY` build then compiles the whole comparison with no DuckDB and no ZeroMQ. A Python driver replaces the bash harness, and each binary reports its own timings so no external timing wrapper is needed.

**Tech Stack:** C++20, CUDA + CUB (ships with the Toolkit), CMake ≥ 3.16, GoogleTest, Python 3.9+ with pandas/numpy, DuckDB (full build only).

**Spec:** `docs/superpowers/specs/2026-08-10-cuda-cleaning-bench-design.md`

## Global Constraints

- **The default CMake triple `MAS_BENCH_ONLY=OFF, MAS_ENABLE_ZMQ=ON, MAS_ENABLE_CUDA=OFF` must reproduce today's build exactly.** All 73 existing C++ tests and 219 existing Python tests stay green and unchanged, except for tests this plan adds. Verify after every task that touches CMake or `core/`.
- **Never edit `bench/results.csv`.** Those numbers are M3-measured. New results go to `bench/results_cuda.csv`.
- **The ZeroMQ layer is kept** (spec §1.2). Never delete `core/src/transport/`, `core/src/agent/`, `mas_worker`, or `mas_coordinator`. `MAS_ENABLE_ZMQ=OFF` compiles them out; that is the only permitted mechanism.
- `NUM_HEADS` is `36`, defined in `core/include/mas/domain/CapEvent.hpp`. Never hard-code `36`.
- CSV layout is **grouped, not interleaved**: `timestamp`, `H01..H36 Count`, `H01..H36 AppTorque`, `H01..H36 Status` = 109 columns.
- Emission order everywhere is **`(row ascending, head ascending)`**. Never sort to make a comparison pass; a sort would hide a real ordering bug.
- No `mmap`, no `unistd.h`, no `getrusage` outside `platform_metrics.hpp`, no `/usr/bin/time`. `core/` currently has zero POSIX-only includes and must keep it that way.
- C++20 features limited to what MSVC 19.3x (VS 2022) ships. No compiler intrinsics, no GCC/Clang pragmas.
- Build directory for local verification: use `build-plan/` for scratch configures so the existing `build/` is never disturbed.
- Test binary is `build/unit_tests`; Python tests run as `.venv/bin/python -m pytest python/tests -q` from the repo root.

---

## File Structure

| File | Responsibility |
|---|---|
| `core/include/mas/domain/CapEventExtractorFlat.hpp` | Element-wise transform + CSV→columns loader, declarations |
| `core/src/domain/CapEventExtractorFlat.cpp` | Their implementation. Stdlib only. |
| `core/include/mas/util/platform_metrics.hpp` | Header-only wall/CPU/peak-RSS. The only `#ifdef _WIN32` in the repo. |
| `core/src/apps/bench_cpu_main.cpp` | Store-free C++ contender, 1T and MT |
| `core/cuda/CudaCleaner.hpp` | CUDA pipeline interface, host-callable, no CUDA types leaked |
| `core/cuda/CudaCleaner.cu` | Kernels S2–S5 + host orchestration |
| `core/src/apps/cuda_clean_main.cpp` | CLI for the CUDA pipeline |
| `python/clean_vectorized.py` | pandas/numpy contender |
| `bench/run_bench_cuda.py` | The driver: extract, run, verify, emit CSVs |
| `bench/requirements-bench.txt` | pandas, numpy, matplotlib |
| `bench/README.md` | Windows/Linux/macOS run instructions |
| `tests/test_cap_event_extractor_flat.cpp` | T1 (the gate), T5-cpp, T8 |
| `tests/test_bench_cpu_parity.cpp` | T7 |
| `python/tests/test_clean_vectorized.py` | T2, T5-py |
| `.gitattributes` | Pin `*.csv` and `*.sh` to LF |
| `CMakeLists.txt` | Target split, three options, CUDA block, Windows DuckDB |

---

## Task 1: The gate — element-wise extractor and column loader

This is spec §3's proof. **If Step 4 fails, stop and report; the plan is abandoned per spec §1.1, not patched.** Runs entirely on macOS/Linux, no GPU.

**Files:**
- Create: `core/include/mas/domain/CapEventExtractorFlat.hpp`
- Create: `core/src/domain/CapEventExtractorFlat.cpp`
- Create: `tests/test_cap_event_extractor_flat.cpp`
- Create: `.gitattributes`
- Modify: `CMakeLists.txt:66-74` (add source to `mas_core`), `CMakeLists.txt:103-114` (add test file)

**Interfaces:**
- Consumes: `mas::CapEvent`, `mas::RawRow`, `mas::NUM_HEADS`, `mas::is_reject` from `mas/domain/CapEvent.hpp`
- Produces: `mas::extract_flat(...)`, `mas::RawColumns`, `mas::load_columns(...)` — used by Tasks 7 (CUDA reference) and referenced by Task 9

- [ ] **Step 1: Write the header**

Create `core/include/mas/domain/CapEventExtractorFlat.hpp`:

```cpp
#pragma once
#include "mas/domain/CapEvent.hpp"
#include <cstddef>
#include <string>
#include <vector>

namespace mas {

// Raw telemetry CSV loaded into column arrays. count/torque/status are
// row-major [n_rows][NUM_HEADS]; ts has one entry per row.
struct RawColumns {
    std::vector<std::string> ts;
    std::vector<double> count, torque, status;
    std::size_t n_rows = 0;
};

// The 109 column names the pool uses (spec §4): "timestamp", then
// H01..H36 Count, H01..H36 AppTorque, H01..H36 Status.
std::vector<std::string> expected_header();

// Load `path` into `out`. Returns false and fills `error` if the file cannot be
// opened or its header is not expected_header(). Tolerates CRLF line endings.
bool load_columns(const std::string& path, RawColumns& out, std::string& error);

// Element-wise form of CapEventExtractor (spec §3): last_count_[h] after row i
// always equals llround(count[i][h]), so the transform never reads state older
// than the previous row. Row 0 is the seed and emits nothing. Appends to `out`
// in (row asc, head asc) order -- identical to CapEventExtractor's order.
void extract_flat(const std::vector<std::string>& ts,
                  const double* count, const double* torque, const double* status,
                  std::size_t n_rows, std::vector<CapEvent>& out);

} // namespace mas
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_cap_event_extractor_flat.cpp`:

```cpp
#include "mas/domain/CapEventExtractor.hpp"
#include "mas/domain/CapEventExtractorFlat.hpp"
#include <gtest/gtest.h>
#include <cstdio>
#include <fstream>
#include <initializer_list>
#include <string>
#include <utility>
#include <vector>

namespace {

mas::RawRow makeRow(const std::string& ts,
                    std::initializer_list<std::pair<int, long long>> caps,
                    double torque = 2.0, double status = 2.0) {
    mas::RawRow r;
    r.ts = ts;
    for (const auto& [head, count] : caps) {
        r.count[head - 1] = static_cast<double>(count);
        r.torque[head - 1] = torque;
        r.status[head - 1] = status;
    }
    return r;
}

std::vector<mas::CapEvent> stateful(const std::vector<mas::RawRow>& rows) {
    mas::CapEventExtractor ex;
    std::vector<mas::CapEvent> out;
    for (const auto& r : rows) ex.process(r, out);
    return out;
}

std::vector<mas::CapEvent> flat(const std::vector<mas::RawRow>& rows) {
    std::vector<std::string> ts;
    std::vector<double> count, torque, status;
    for (const auto& r : rows) {
        ts.push_back(r.ts);
        for (int h = 0; h < mas::NUM_HEADS; ++h) {
            count.push_back(r.count[h]);
            torque.push_back(r.torque[h]);
            status.push_back(r.status[h]);
        }
    }
    std::vector<mas::CapEvent> out;
    mas::extract_flat(ts, count.data(), torque.data(), status.data(), rows.size(), out);
    return out;
}

void expectSame(const std::vector<mas::CapEvent>& a,
                const std::vector<mas::CapEvent>& b) {
    ASSERT_EQ(a.size(), b.size());
    for (std::size_t i = 0; i < a.size(); ++i) {
        SCOPED_TRACE("event index " + std::to_string(i));
        EXPECT_EQ(a[i].head_id, b[i].head_id);
        EXPECT_EQ(a[i].ts, b[i].ts);
        EXPECT_EQ(a[i].cap_seq, b[i].cap_seq);
        EXPECT_DOUBLE_EQ(a[i].app_torque, b[i].app_torque);
        EXPECT_DOUBLE_EQ(a[i].status, b[i].status);
        EXPECT_EQ(a[i].delta, b[i].delta);
        EXPECT_EQ(a[i].is_fault, b[i].is_fault);
        EXPECT_EQ(a[i].aggregated, b[i].aggregated);
        EXPECT_EQ(a[i].reset, b[i].reset);
    }
}

void expectAgrees(const std::vector<mas::RawRow>& rows) {
    expectSame(stateful(rows), flat(rows));
}

} // namespace

TEST(ExtractorFlat, EmptyInput)      { expectAgrees({}); }
TEST(ExtractorFlat, SingleSeedRow)   { expectAgrees({makeRow("t0", {{1, 100}})}); }

TEST(ExtractorFlat, SingleIncrement) {
    expectAgrees({makeRow("t0", {{1, 100}}), makeRow("t1", {{1, 101}})});
}

TEST(ExtractorFlat, HeldRunEmitsNothing) {
    expectAgrees({makeRow("t0", {{1, 100}}), makeRow("t1", {{1, 100}}),
                  makeRow("t2", {{1, 100}}), makeRow("t3", {{1, 100}})});
}

TEST(ExtractorFlat, AggregatedDeltaGreaterThanOne) {
    expectAgrees({makeRow("t0", {{1, 100}}), makeRow("t1", {{1, 105}})});
}

TEST(ExtractorFlat, CounterReset) {
    expectAgrees({makeRow("t0", {{1, 900}}), makeRow("t1", {{1, 3}})});
}

TEST(ExtractorFlat, ResetThenAdvance) {
    expectAgrees({makeRow("t0", {{1, 900}}), makeRow("t1", {{1, 3}}),
                  makeRow("t2", {{1, 4}}),   makeRow("t3", {{1, 4}}),
                  makeRow("t4", {{1, 9}})});
}

TEST(ExtractorFlat, AllHeadsFireSimultaneously) {
    std::vector<std::pair<int, long long>> a, b;
    for (int h = 1; h <= mas::NUM_HEADS; ++h) { a.push_back({h, 10}); b.push_back({h, 11}); }
    mas::RawRow r0, r1;
    r0.ts = "t0"; r1.ts = "t1";
    for (int h = 0; h < mas::NUM_HEADS; ++h) {
        r0.count[h] = 10; r0.torque[h] = 2.5; r0.status[h] = 0;
        r1.count[h] = 11; r1.torque[h] = 2.5; r1.status[h] = 0;
    }
    expectAgrees({r0, r1});
}

TEST(ExtractorFlat, RejectAndNoLoadStatusesAgree) {
    // status 65 = Bad Closure (reject), 9 = No InTorque (reject), 2 = No Load,
    // 4 = No Closure (not a reject). is_fault must match on every one.
    expectAgrees({makeRow("t0", {{2, 50}}, 2.0, 0.0),
                  makeRow("t1", {{2, 51}}, 1.9, 65.0),
                  makeRow("t2", {{2, 52}}, 1.8, 9.0),
                  makeRow("t3", {{2, 53}}, 0.0, 2.0),
                  makeRow("t4", {{2, 54}}, 0.5, 4.0)});
}

TEST(ExtractorFlat, HeaderIsRejectedWhenColumnsAreWrong) {
    const std::string p = "flat_bad_header.csv";
    { std::ofstream f(p); f << "timestamp,nope,alsonope\n1,2,3\n"; }
    mas::RawColumns cols; std::string err;
    EXPECT_FALSE(mas::load_columns(p, cols, err));
    EXPECT_NE(err.find("column 1"), std::string::npos) << "error was: " << err;
    std::remove(p.c_str());
}

TEST(ExtractorFlat, MissingFileIsReported) {
    mas::RawColumns cols; std::string err;
    EXPECT_FALSE(mas::load_columns("definitely_not_here.csv", cols, err));
    EXPECT_FALSE(err.empty());
}

// Helper: write a 2-row CSV with the real header, using `eol` as the terminator.
namespace {
void writeTiny(const std::string& path, const std::string& eol) {
    std::ofstream f(path, std::ios::binary);
    const auto hdr = mas::expected_header();
    for (std::size_t i = 0; i < hdr.size(); ++i) f << (i ? "," : "") << hdr[i];
    f << eol;
    for (int row = 0; row < 2; ++row) {
        f << "2026-02-01T00:00:0" << row << ".000";
        for (int h = 0; h < mas::NUM_HEADS; ++h) f << "," << (100 + row);
        for (int h = 0; h < mas::NUM_HEADS; ++h) f << ",2.54";
        for (int h = 0; h < mas::NUM_HEADS; ++h) f << ",0";
        f << eol;
    }
}
} // namespace

TEST(ExtractorFlat, CrlfYieldsIdenticalEventsToLf) {
    writeTiny("flat_lf.csv", "\n");
    writeTiny("flat_crlf.csv", "\r\n");
    mas::RawColumns a, b; std::string err;
    ASSERT_TRUE(mas::load_columns("flat_lf.csv", a, err)) << err;
    ASSERT_TRUE(mas::load_columns("flat_crlf.csv", b, err)) << err;
    std::vector<mas::CapEvent> ea, eb;
    mas::extract_flat(a.ts, a.count.data(), a.torque.data(), a.status.data(), a.n_rows, ea);
    mas::extract_flat(b.ts, b.count.data(), b.torque.data(), b.status.data(), b.n_rows, eb);
    ASSERT_EQ(ea.size(), static_cast<std::size_t>(mas::NUM_HEADS));
    expectSame(ea, eb);
    std::remove("flat_lf.csv"); std::remove("flat_crlf.csv");
}

// The real-data gate. Skipped when the pool has not been extracted.
TEST(ExtractorFlat, AgreesWithStatefulExtractorOnARealDayFile) {
    const std::string p =
        "telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02/"
        "telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02-01.csv";
    std::ifstream probe(p);
    if (!probe.good()) GTEST_SKIP() << "pool not extracted: " << p;
    probe.close();

    mas::RawColumns cols; std::string err;
    ASSERT_TRUE(mas::load_columns(p, cols, err)) << err;

    std::vector<mas::RawRow> rows(cols.n_rows);
    for (std::size_t i = 0; i < cols.n_rows; ++i) {
        rows[i].ts = cols.ts[i];
        for (int h = 0; h < mas::NUM_HEADS; ++h) {
            rows[i].count[h]  = cols.count[i * mas::NUM_HEADS + h];
            rows[i].torque[h] = cols.torque[i * mas::NUM_HEADS + h];
            rows[i].status[h] = cols.status[i * mas::NUM_HEADS + h];
        }
    }
    const auto want = stateful(rows);
    std::vector<mas::CapEvent> got;
    mas::extract_flat(cols.ts, cols.count.data(), cols.torque.data(),
                      cols.status.data(), cols.n_rows, got);
    EXPECT_GT(want.size(), 100000u) << "day-file should yield ~765k events";
    expectSame(want, got);
}
```

- [ ] **Step 3: Wire the new files into CMake, then run to verify the test fails**

In `CMakeLists.txt`, add to the `mas_core` source list (after `core/src/domain/CapEventExtractor.cpp`):

```cmake
  core/src/domain/CapEventExtractorFlat.cpp
```

and add to the `unit_tests` source list (after `tests/test_cap_event_extractor.cpp`):

```cmake
  tests/test_cap_event_extractor_flat.cpp
```

Run: `cmake -S . -B build && cmake --build build -j 2>&1 | tail -5`
Expected: **link error** — `undefined symbol: mas::extract_flat`, `mas::load_columns`, `mas::expected_header`. That is the failing state; the implementation file does not exist yet.

- [ ] **Step 4: Write the implementation**

Create `core/src/domain/CapEventExtractorFlat.cpp`:

```cpp
#include "mas/domain/CapEventExtractorFlat.hpp"
#include <cmath>
#include <fstream>
#include <sstream>

namespace mas {

namespace {

std::string pad2(int n) {
    return (n < 10 ? "0" : "") + std::to_string(n);
}

// Split on ',' and drop a trailing CR. Kept local: load_columns is the only
// caller, and CsvRawReader has its own splitter with different error semantics.
std::vector<std::string> splitLine(const std::string& line) {
    std::vector<std::string> f;
    f.reserve(1 + NUM_HEADS * 3);
    std::string cur;
    std::istringstream ss(line);
    while (std::getline(ss, cur, ',')) {
        if (!cur.empty() && cur.back() == '\r') cur.pop_back();
        f.push_back(cur);
    }
    return f;
}

} // namespace

std::vector<std::string> expected_header() {
    std::vector<std::string> h;
    h.reserve(1 + NUM_HEADS * 3);
    h.push_back("timestamp");
    for (int i = 1; i <= NUM_HEADS; ++i) h.push_back("H" + pad2(i) + " Count");
    for (int i = 1; i <= NUM_HEADS; ++i) h.push_back("H" + pad2(i) + " AppTorque");
    for (int i = 1; i <= NUM_HEADS; ++i) h.push_back("H" + pad2(i) + " Status");
    return h;
}

bool load_columns(const std::string& path, RawColumns& out, std::string& error) {
    std::ifstream in(path, std::ios::binary);
    if (!in.is_open()) { error = "cannot open " + path; return false; }

    std::string line;
    if (!std::getline(in, line)) { error = path + " is empty"; return false; }
    const auto want = expected_header();
    const auto got = splitLine(line);
    if (got.size() != want.size()) {
        error = path + ": header has " + std::to_string(got.size()) +
                " columns, expected " + std::to_string(want.size());
        return false;
    }
    for (std::size_t i = 0; i < want.size(); ++i) {
        if (got[i] != want[i]) {
            error = path + ": column " + std::to_string(i) + " is '" + got[i] +
                    "', expected '" + want[i] + "'";
            return false;
        }
    }

    out = RawColumns{};
    while (std::getline(in, line)) {
        if (line.empty() || line == "\r") continue;
        const auto f = splitLine(line);
        if (f.size() < static_cast<std::size_t>(1 + NUM_HEADS * 3)) continue;
        try {
            out.ts.push_back(f[0]);
            for (int h = 0; h < NUM_HEADS; ++h) out.count.push_back(std::stod(f[1 + h]));
            for (int h = 0; h < NUM_HEADS; ++h) out.torque.push_back(std::stod(f[1 + NUM_HEADS + h]));
            for (int h = 0; h < NUM_HEADS; ++h) out.status.push_back(std::stod(f[1 + 2 * NUM_HEADS + h]));
        } catch (const std::exception&) {
            out.ts.resize(out.n_rows);                    // roll the partial row back
            out.count.resize(out.n_rows * NUM_HEADS);
            out.torque.resize(out.n_rows * NUM_HEADS);
            out.status.resize(out.n_rows * NUM_HEADS);
            continue;
        }
        ++out.n_rows;
    }
    return true;
}

void extract_flat(const std::vector<std::string>& ts,
                  const double* count, const double* torque, const double* status,
                  std::size_t n_rows, std::vector<CapEvent>& out) {
    for (std::size_t i = 1; i < n_rows; ++i) {
        const std::size_t cur = i * NUM_HEADS;
        const std::size_t prv = (i - 1) * NUM_HEADS;
        for (int h = 0; h < NUM_HEADS; ++h) {
            const long long c_cur = std::llround(count[cur + h]);
            const long long c_prv = std::llround(count[prv + h]);
            if (c_cur == c_prv) continue;          // held: emit nothing

            CapEvent e;
            e.head_id = h + 1;
            e.ts = ts[i];
            e.cap_seq = c_cur;
            e.app_torque = torque[cur + h];
            e.status = status[cur + h];
            e.delta = (c_cur > c_prv) ? static_cast<int>(c_cur - c_prv) : 0;
            e.is_fault = is_reject(status[cur + h]);
            e.aggregated = e.delta > 1;
            e.reset = c_cur < c_prv;
            out.push_back(e);
        }
    }
}

} // namespace mas
```

- [ ] **Step 5: Run the gate**

Run: `cmake --build build -j && build/unit_tests --gtest_filter='ExtractorFlat.*'`
Expected: **PASS**, 12 tests. `AgreesWithStatefulExtractorOnARealDayFile` must PASS, not SKIP — if it skips, extract the pool first:
`python3 -c "import zipfile; zipfile.ZipFile('telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02.zip').extractall('telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02')"`

**If `expectSame` fails on the real day-file, STOP.** Report the first differing event. Spec §1.1 and §10 R1 apply: the plan is abandoned, not reworked into a scan-based kernel.

- [ ] **Step 6: Add `.gitattributes`**

Create `.gitattributes`:

```
*.csv text eol=lf
*.sh  text eol=lf
*.py  text eol=lf
*.cu  text eol=lf
```

- [ ] **Step 7: Confirm no regression**

Run: `build/unit_tests 2>&1 | tail -3`
Expected: `[  PASSED  ] 86 tests.` (73 existing + 13 new)

- [ ] **Step 8: Commit**

```bash
git add core/include/mas/domain/CapEventExtractorFlat.hpp \
        core/src/domain/CapEventExtractorFlat.cpp \
        tests/test_cap_event_extractor_flat.cpp .gitattributes CMakeLists.txt
git commit -m "feat(domain): element-wise extractor, proved against the stateful one

CapEventExtractor::process leaves last_count_[h] equal to the current row's
count in every branch, so it never reads state older than one row back. That
makes the transform element-wise over consecutive row pairs rather than 36
sequential chains -- the precondition for the GPU port.

The differential test is the claim: same events, all nine fields, on a real
day-file as well as on the edge cases."
```

---

## Task 2: Split `mas_core` so the cleaning path links nothing

**Files:**
- Modify: `CMakeLists.txt:66-118`

**Interfaces:**
- Consumes: nothing new
- Produces: CMake targets `mas_clean_core` (no deps), `mas_store` (DuckDB), `mas_agent`, `mas_transport` — Tasks 3, 4, 5, 7 link `mas_clean_core`

- [ ] **Step 1: Replace the target block**

In `CMakeLists.txt`, replace lines 66-80 (`add_library(mas_core ...)` through `target_link_libraries(mas_transport ...)`) with:

```cmake
# Cleaning hot path: C++20 stdlib only, no DuckDB, no ZeroMQ. This is what the
# portable benchmark build (MAS_BENCH_ONLY) compiles, and the reason the split
# exists -- Pipeline.cpp used to drag duckdb_imported into anything that cleaned.
add_library(mas_clean_core
  core/src/domain/CapEventExtractor.cpp
  core/src/domain/CapEventExtractorFlat.cpp
  core/src/store/CsvRawReader.cpp)
target_include_directories(mas_clean_core PUBLIC core/include)

add_library(mas_store
  core/src/store/CsvEventStore.cpp
  core/src/store/DuckDbEventStore.cpp
  core/src/domain/Pipeline.cpp)
target_link_libraries(mas_store PUBLIC mas_clean_core PRIVATE duckdb_imported)

add_library(mas_agent
  core/src/agent/Message.cpp
  core/src/agent/CleaningWorker.cpp
  core/src/agent/Coordinator.cpp)
target_link_libraries(mas_agent PUBLIC mas_store)

# Back-compat alias so existing target_link_libraries(... mas_core) keeps working.
add_library(mas_core INTERFACE)
target_link_libraries(mas_core INTERFACE mas_agent)

add_library(mas_transport core/src/transport/ZmqTransport.cpp)
target_include_directories(mas_transport PUBLIC core/include)
target_link_libraries(mas_transport PUBLIC cppzmq)
```

- [ ] **Step 2: Build and run the full suite**

Run: `cmake -S . -B build && cmake --build build -j && build/unit_tests 2>&1 | tail -3`
Expected: `[  PASSED  ] 86 tests.` — the split is mechanical; no source file changes.

- [ ] **Step 3: Verify `mas_clean_core` really has no dependencies**

Run: `cmake --build build --target mas_clean_core -j 2>&1 | tail -3 && nm -u build/libmas_clean_core.a 2>/dev/null | grep -ci duckdb || echo "0 duckdb symbols"`
Expected: builds, and `0 duckdb symbols`.

- [ ] **Step 4: Commit**

```bash
git add CMakeLists.txt
git commit -m "refactor(build): split mas_core so cleaning links nothing

Pipeline.cpp lives with the DuckDB stores, so every consumer of the cleaning
hot path was also linking duckdb_imported. mas_clean_core is now stdlib-only,
which is what lets the benchmark build on a machine with no DuckDB.

mas_core survives as an INTERFACE alias -- no call site changes."
```

---

## Task 3: `platform_metrics.hpp`

**Files:**
- Create: `core/include/mas/util/platform_metrics.hpp`
- Create: `tests/test_platform_metrics.cpp`
- Modify: `CMakeLists.txt` (add test file to `unit_tests`)

**Interfaces:**
- Produces: `mas::ProcMetrics { double wall_s, cpu_s, peak_rss_mb; }`, `mas::read_metrics()`, `mas::metrics_line(const std::string& tag, const ProcMetrics&)` — Tasks 4, 5, 7 print with it; Task 9 parses it

- [ ] **Step 1: Write the failing test**

Create `tests/test_platform_metrics.cpp`:

```cpp
#include "mas/util/platform_metrics.hpp"
#include <gtest/gtest.h>
#include <string>
#include <vector>

TEST(PlatformMetrics, ReportsPlausibleValues) {
    // Touch some memory so peak RSS is certainly non-trivial.
    std::vector<double> ballast(4 * 1024 * 1024, 1.5);
    volatile double sink = 0;
    for (std::size_t i = 0; i < ballast.size(); i += 1024) sink += ballast[i];
    (void)sink;

    const auto m = mas::read_metrics();
    EXPECT_GE(m.wall_s, 0.0);
    EXPECT_GE(m.cpu_s, 0.0);
    EXPECT_GT(m.peak_rss_mb, 1.0) << "32 MB ballast should show in peak RSS";
}

TEST(PlatformMetrics, LineIsParseable) {
    const mas::ProcMetrics m{1.25, 2.5, 128.0};
    EXPECT_EQ(mas::metrics_line("clean", m),
              "metrics: tag=clean wall_s=1.250 cpu_s=2.500 peak_rss_mb=128.0");
}
```

Add `tests/test_platform_metrics.cpp` to the `unit_tests` source list in `CMakeLists.txt`.

- [ ] **Step 2: Run to verify it fails**

Run: `cmake -S . -B build && cmake --build build -j 2>&1 | tail -5`
Expected: FAIL — `fatal error: 'mas/util/platform_metrics.hpp' file not found`

- [ ] **Step 3: Write the header**

Create `core/include/mas/util/platform_metrics.hpp`:

```cpp
#pragma once
// Self-reported process metrics. Replaces the external timing wrapper:
// bench/run_bench.sh used `/usr/bin/time -l`, which is BSD-only -- the flag
// does not exist in GNU coreutils and has no Windows equivalent at all.
// This is the only #ifdef _WIN32 in the codebase (spec §10 R3).
#include <chrono>
#include <cstdio>
#include <string>

#ifdef _WIN32
#  ifndef WIN32_LEAN_AND_MEAN
#    define WIN32_LEAN_AND_MEAN
#  endif
#  include <windows.h>
#  include <psapi.h>
#else
#  include <sys/resource.h>
#  include <sys/time.h>
#endif

namespace mas {

struct ProcMetrics {
    double wall_s = 0.0;
    double cpu_s = 0.0;        // user + system
    double peak_rss_mb = 0.0;
};

namespace detail {
inline std::chrono::steady_clock::time_point& start_time() {
    static std::chrono::steady_clock::time_point t0 = std::chrono::steady_clock::now();
    return t0;
}
} // namespace detail

// Call once at the top of main() to anchor wall_s. Safe to omit -- the anchor
// is then the first read_metrics() call.
inline void metrics_init() { (void)detail::start_time(); }

inline ProcMetrics read_metrics() {
    ProcMetrics m;
    m.wall_s = std::chrono::duration<double>(
        std::chrono::steady_clock::now() - detail::start_time()).count();
#ifdef _WIN32
    FILETIME creation, exit, kernel, user;
    if (GetProcessTimes(GetCurrentProcess(), &creation, &exit, &kernel, &user)) {
        auto to_s = [](const FILETIME& ft) {
            ULARGE_INTEGER v;
            v.LowPart = ft.dwLowDateTime;
            v.HighPart = ft.dwHighDateTime;
            return static_cast<double>(v.QuadPart) * 1e-7;   // 100 ns ticks
        };
        m.cpu_s = to_s(kernel) + to_s(user);
    }
    PROCESS_MEMORY_COUNTERS pmc{};
    if (GetProcessMemoryInfo(GetCurrentProcess(), &pmc, sizeof(pmc)))
        m.peak_rss_mb = static_cast<double>(pmc.PeakWorkingSetSize) / 1048576.0;
#else
    struct rusage ru {};
    if (getrusage(RUSAGE_SELF, &ru) == 0) {
        m.cpu_s = static_cast<double>(ru.ru_utime.tv_sec + ru.ru_stime.tv_sec) +
                  1e-6 * static_cast<double>(ru.ru_utime.tv_usec + ru.ru_stime.tv_usec);
        // ru_maxrss is bytes on macOS, kilobytes on Linux.
#  ifdef __APPLE__
        m.peak_rss_mb = static_cast<double>(ru.ru_maxrss) / 1048576.0;
#  else
        m.peak_rss_mb = static_cast<double>(ru.ru_maxrss) / 1024.0;
#  endif
    }
#endif
    return m;
}

// One machine-readable line on stderr, parsed by bench/run_bench_cuda.py.
inline std::string metrics_line(const std::string& tag, const ProcMetrics& m) {
    char buf[256];
    std::snprintf(buf, sizeof(buf),
                  "metrics: tag=%s wall_s=%.3f cpu_s=%.3f peak_rss_mb=%.1f",
                  tag.c_str(), m.wall_s, m.cpu_s, m.peak_rss_mb);
    return std::string(buf);
}

} // namespace mas
```

On Windows, link `psapi`. Add to `CMakeLists.txt` right after the `mas_clean_core` target:

```cmake
if(WIN32)
  target_link_libraries(mas_clean_core PUBLIC psapi)
endif()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cmake --build build -j && build/unit_tests --gtest_filter='PlatformMetrics.*'`
Expected: PASS, 2 tests.

- [ ] **Step 5: Commit**

```bash
git add core/include/mas/util/platform_metrics.hpp tests/test_platform_metrics.cpp CMakeLists.txt
git commit -m "feat(util): self-reported process metrics, portable

bench/run_bench.sh wraps every run in /usr/bin/time -l. That flag is BSD-only:
GNU coreutils rejects it and Windows has no equivalent. Having each binary
report its own wall, CPU, and peak RSS works everywhere and is more accurate,
since it excludes process spawn."
```

---

## Task 4: `bench_cpu` — the store-free C++ contender

**Files:**
- Create: `core/src/apps/bench_cpu_main.cpp`
- Create: `tests/test_bench_cpu_parity.cpp`
- Modify: `CMakeLists.txt` (add executable + test file)

**Interfaces:**
- Consumes: `mas::CsvRawReader`, `mas::CapEventExtractor`, `mas::read_metrics`, `mas::metrics_line`
- Produces: binary `bench_cpu`, stdout line `bench_cpu: <nfiles> files, <events> events, clean <s> s` — parsed by Task 9

- [ ] **Step 1: Write the failing parity test**

Create `tests/test_bench_cpu_parity.cpp`. This is T7 — it asserts `bench_cpu`'s in-memory path produces the same events as `Pipeline::clean_file`, so substituting it for `mas_monolith` in the sweep is auditable:

```cpp
#include "mas/domain/CapEventExtractor.hpp"
#include "mas/domain/CapEventExtractorFlat.hpp"
#include "mas/store/CsvRawReader.hpp"
#include <gtest/gtest.h>
#include <fstream>
#include <string>
#include <vector>

namespace {

// The exact loop bench_cpu_main.cpp runs per file. Duplicated here on purpose:
// the test asserts the loop's behaviour, so it must not import the binary's
// internals and assert against itself.
std::vector<mas::CapEvent> cleanInMemory(const std::string& path) {
    mas::CsvRawReader reader(path);
    std::vector<mas::CapEvent> out;
    if (!reader.is_open()) return out;
    mas::CapEventExtractor ex;
    mas::RawRow row;
    while (reader.next(row)) ex.process(row, out);
    return out;
}

} // namespace

TEST(BenchCpuParity, MatchesLoadColumnsPlusFlatOnARealDayFile) {
    const std::string p =
        "telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02/"
        "telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02-01.csv";
    std::ifstream probe(p);
    if (!probe.good()) GTEST_SKIP() << "pool not extracted: " << p;
    probe.close();

    const auto streamed = cleanInMemory(p);

    mas::RawColumns cols; std::string err;
    ASSERT_TRUE(mas::load_columns(p, cols, err)) << err;
    std::vector<mas::CapEvent> flat;
    mas::extract_flat(cols.ts, cols.count.data(), cols.torque.data(),
                      cols.status.data(), cols.n_rows, flat);

    ASSERT_EQ(streamed.size(), flat.size());
    for (std::size_t i = 0; i < streamed.size(); ++i) {
        SCOPED_TRACE("event index " + std::to_string(i));
        EXPECT_EQ(streamed[i].head_id, flat[i].head_id);
        EXPECT_EQ(streamed[i].ts, flat[i].ts);
        EXPECT_EQ(streamed[i].cap_seq, flat[i].cap_seq);
        EXPECT_DOUBLE_EQ(streamed[i].app_torque, flat[i].app_torque);
        EXPECT_EQ(streamed[i].delta, flat[i].delta);
        EXPECT_EQ(streamed[i].reset, flat[i].reset);
    }
}
```

Add `tests/test_bench_cpu_parity.cpp` to `unit_tests` in `CMakeLists.txt`.

- [ ] **Step 2: Run to verify it fails or skips honestly**

Run: `cmake -S . -B build && cmake --build build -j && build/unit_tests --gtest_filter='BenchCpuParity.*'`
Expected: PASS if the pool is extracted (the pieces already exist from Task 1); SKIP otherwise. If it SKIPs, extract the pool with the command in Task 1 Step 5 and re-run — this test must actually run before you continue.

- [ ] **Step 3: Write `bench_cpu`**

Create `core/src/apps/bench_cpu_main.cpp`:

```cpp
// Store-free C++ contender for the CUDA benchmark (spec §5.4). Mirrors
// monolith_main.cpp's file-grain threading -- a fixed pool of T threads pulling
// file indices off an atomic counter -- but accumulates events in memory instead
// of writing DuckDB. Same CsvRawReader -> CapEventExtractor hot path.
//
// It exists so the headline measurement does not require DuckDB on the target
// machine. tests/test_bench_cpu_parity.cpp keeps the substitution honest.
#include "mas/domain/CapEventExtractor.hpp"
#include "mas/store/CsvRawReader.hpp"
#include "mas/util/platform_metrics.hpp"
#include <atomic>
#include <chrono>
#include <cstddef>
#include <exception>
#include <iomanip>
#include <iostream>
#include <string>
#include <thread>
#include <vector>

namespace {

long long cleanOne(const std::string& path) {
    mas::CsvRawReader reader(path);
    if (!reader.is_open()) return -1;
    mas::CapEventExtractor ex;
    mas::RawRow row;
    std::vector<mas::CapEvent> batch;
    long long n = 0;
    constexpr std::size_t kBatch = 8192;   // matches Pipeline::clean_file
    while (reader.next(row)) {
        ex.process(row, batch);
        if (batch.size() >= kBatch) { n += static_cast<long long>(batch.size()); batch.clear(); }
    }
    n += static_cast<long long>(batch.size());
    return n;
}

} // namespace

int main(int argc, char** argv) {
    mas::metrics_init();
    if (argc < 3) {
        std::cerr << "usage: bench_cpu <threads> <day1.csv> [day2.csv ...]\n";
        return 2;
    }
    int threads = 0;
    try { threads = std::stoi(argv[1]); }
    catch (const std::exception&) { std::cerr << "error: threads must be a number\n"; return 2; }
    if (threads < 1) { std::cerr << "error: threads must be >= 1\n"; return 2; }

    std::vector<std::string> files;
    for (int i = 2; i < argc; ++i) files.emplace_back(argv[i]);

    const auto t0 = std::chrono::steady_clock::now();
    std::vector<long long> per_file(files.size(), 0);
    std::atomic<std::size_t> next{0};

    auto pull = [&] {
        for (std::size_t i; (i = next.fetch_add(1)) < files.size();)
            per_file[i] = cleanOne(files[i]);
    };
    {
        std::vector<std::thread> pool;
        pool.reserve(static_cast<std::size_t>(threads));
        for (int t = 0; t < threads; ++t) pool.emplace_back(pull);
        for (auto& th : pool) th.join();
    }
    const double clean_s =
        std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();

    long long events = 0;
    for (std::size_t i = 0; i < files.size(); ++i) {
        if (per_file[i] < 0) { std::cerr << "error: cannot clean " << files[i] << "\n"; return 1; }
        events += per_file[i];
    }

    std::cerr << "bench_cpu: " << files.size() << " files, " << events
              << " events, clean " << std::fixed << std::setprecision(3)
              << clean_s << " s\n";
    std::cerr << mas::metrics_line("clean", mas::read_metrics()) << "\n";
    return 0;
}
```

Add to `CMakeLists.txt` after the `monolith_exe` block:

```cmake
add_executable(bench_cpu_exe core/src/apps/bench_cpu_main.cpp)
target_link_libraries(bench_cpu_exe PRIVATE mas_clean_core Threads::Threads)
set_target_properties(bench_cpu_exe PROPERTIES OUTPUT_NAME bench_cpu)
```

- [ ] **Step 4: Verify it runs and agrees with the oracle**

Run:
```bash
cmake --build build -j
D=telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02
build/bench_cpu 1 $D/telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02-01.csv
python3 python/oracle.py $D/telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02-01.csv
```
Expected: `bench_cpu` prints `765711 events` and a `metrics:` line; `oracle.py` prints `765711`. The two numbers must match exactly.

- [ ] **Step 5: Commit**

```bash
git add core/src/apps/bench_cpu_main.cpp tests/test_bench_cpu_parity.cpp CMakeLists.txt
git commit -m "feat(bench): store-free C++ contender

Requiring DuckDB on the target machine just to get a C++ number would make the
headline measurement hostage to the store dependency. bench_cpu runs the same
CsvRawReader -> CapEventExtractor hot path with the same file-grain threading
and links only mas_clean_core.

The parity test keeps the substitution auditable rather than assumed."
```

---

## Task 5: `mas_monolith --no-store`

**Files:**
- Modify: `core/src/apps/monolith_main.cpp:22-45` (argument parsing), `:50-60` (1T branch)
- Create: `tests/test_monolith_no_store.cpp` — omitted; this is verified by the Step 3 command below, because the behaviour under test is a whole-binary invocation, not a unit

**Interfaces:**
- Consumes: existing `mas::clean_file`, `mas::IEventStore`
- Produces: `mas_monolith --no-store <threads> <files...>` printing the same `monolith:` summary line with `store holds 0 rows`

- [ ] **Step 1: Add a null store and the flag**

In `core/src/apps/monolith_main.cpp`, add to the anonymous namespace after `seconds_since`:

```cpp
// Counts events and discards them. Lets the benchmark time the clean path
// without DuckDB dominating it (spec §6.1): at ~15 ms of GPU work per day-file,
// the store write is two orders of magnitude larger and would flatten every
// arch into the same number.
class NullEventStore : public mas::IEventStore {
public:
    void write(std::span<const mas::CapEvent> events) override {
        n_ += static_cast<long long>(events.size());
    }
    long long count() const { return n_; }

private:
    long long n_ = 0;
};
```

Add `#include "mas/store/EventStore.hpp"` and `#include <span>` to the includes.

Replace the argument-parsing block (currently `const std::string out = argv[1], machine = argv[2];` and the `threads` parse) with:

```cpp
    int argi = 1;
    bool no_store = false;
    if (std::string(argv[argi]) == "--no-store") { no_store = true; ++argi; }
    if (argc - argi < (no_store ? 3 : 4)) {
        std::cerr << "usage: mas_monolith [--no-store] <out.duckdb> <machine_id> "
                     "<threads> <day1.csv> [day2.csv ...]\n";
        return 2;
    }
    const std::string out = argv[argi++], machine = argv[argi++];
    int threads = 0;
    try {
        threads = std::stoi(argv[argi++]);
    } catch (const std::exception&) {
        std::cerr << "error: threads must be a number\n";
        return 2;
    }
    if (threads < 1) { std::cerr << "error: threads must be >= 1\n"; return 2; }
    if (no_store && threads != 1) {
        std::cerr << "error: --no-store requires threads = 1 "
                     "(the MT path merges per-thread stores)\n";
        return 2;
    }
    std::vector<std::string> files;
    for (int i = argi; i < argc; ++i) files.emplace_back(argv[i]);
```

Delete the old `argc < 5` guard, the old `out`/`machine`/`threads` lines, and the old `for (int i = 4; ...)` loop.

In the `threads == 1` branch, replace the store construction so it can be null:

```cpp
        if (threads == 1) {
            if (no_store) {
                NullEventStore store;
                for (const auto& f : files) {
                    const long long n = mas::clean_file(f, store);
                    if (n < 0) { std::cerr << "error: cannot clean " << f << "\n"; return 1; }
                    events += n;
                }
                clean_s = seconds_since(t0);
                rows = 0;
            } else {
                mas::DuckDbEventStore store(out, machine);
                for (const auto& f : files) {
                    const long long n = mas::clean_file(f, store);
                    if (n < 0) { std::cerr << "error: cannot clean " << f << "\n"; return 1; }
                    events += n;
                }
                clean_s = seconds_since(t0);
                rows = store.count();
            }
        } else {
```

Also add `#include "mas/util/platform_metrics.hpp"`, call `mas::metrics_init();` as the first line of `main`, and print `std::cerr << mas::metrics_line("clean", mas::read_metrics()) << "\n";` immediately after the existing `monolith:` summary line.

- [ ] **Step 2: Build**

Run: `cmake --build build -j 2>&1 | tail -3`
Expected: builds clean.

- [ ] **Step 3: Verify `--no-store` reports the same event count (T6)**

Run:
```bash
D=telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02
F=$D/telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02-01.csv
build/mas_monolith --no-store /tmp/ignored.duckdb MCC 1 $F
build/mas_monolith /tmp/withstore.duckdb MCC 1 $F
rm -f /tmp/withstore.duckdb
```
Expected: both print `765711 events`. The `--no-store` run prints `store holds 0 rows` and is visibly faster.

- [ ] **Step 4: Confirm no regression**

Run: `build/unit_tests 2>&1 | tail -3`
Expected: `[  PASSED  ] 89 tests.`

- [ ] **Step 5: Commit**

```bash
git add core/src/apps/monolith_main.cpp
git commit -m "feat(monolith): --no-store, so the clean path can be timed alone

At the volumes this benchmark runs, the DuckDB write is two orders of magnitude
larger than the transform. Timing them together compresses every architecture
into the same number and measures the store, not the code.

--no-store is 1T only: the MT path's whole shape is per-thread stores and a
merge, so there is nothing coherent to measure without them."
```

---

## Task 6: CMake options and the portable build

**Files:**
- Modify: `CMakeLists.txt` (whole file restructure — options at top, guards around ZMQ/DuckDB blocks)

**Interfaces:**
- Produces: options `MAS_BENCH_ONLY`, `MAS_ENABLE_ZMQ`, `MAS_ENABLE_CUDA`, `MAS_BUILD_TESTS`

- [ ] **Step 1: Add the options block**

Insert after `project(mas_telemetry LANGUAGES CXX)` in `CMakeLists.txt`:

```cmake
option(MAS_BENCH_ONLY "Build only the cleaning core and benchmark binaries" OFF)
option(MAS_ENABLE_ZMQ "Build the ZeroMQ agent runtime" ON)
option(MAS_ENABLE_CUDA "Build the CUDA cleaning pipeline" OFF)
option(MAS_BUILD_TESTS "Build the GoogleTest suite" ON)

# The benchmark build must download nothing: no DuckDB asset, no libzmq source.
# That is the whole point -- it has to configure on a Windows box with only
# VS Build Tools, CMake, and the CUDA Toolkit installed.
if(MAS_BENCH_ONLY)
  set(MAS_ENABLE_ZMQ OFF CACHE BOOL "forced off by MAS_BENCH_ONLY" FORCE)
endif()
```

- [ ] **Step 2: Guard the dependency fetches**

Wrap the GoogleTest `FetchContent` block (lines 10-14) in `if(MAS_BUILD_TESTS) ... endif()`.

Wrap the ZeroMQ block (lines 16-36) in `if(MAS_ENABLE_ZMQ) ... endif()`.

Wrap the DuckDB block (lines 38-64) in `if(NOT MAS_BENCH_ONLY) ... endif()`.

- [ ] **Step 3: Guard the targets**

Wrap `mas_store`, `mas_agent`, `mas_core`, `clean_exe`, `merge_exe`, `monolith_exe` in `if(NOT MAS_BENCH_ONLY) ... endif()`.

Wrap `mas_transport`, `worker_exe`, `coordinator_exe` in `if(MAS_ENABLE_ZMQ) ... endif()`.

Replace the `unit_tests` block with:

```cmake
if(MAS_BUILD_TESTS)
  enable_testing()
  set(MAS_TEST_SOURCES
    tests/test_cap_event.cpp
    tests/test_cap_event_extractor.cpp
    tests/test_cap_event_extractor_flat.cpp
    tests/test_platform_metrics.cpp
    tests/test_csv_raw_reader.cpp
    tests/test_bench_cpu_parity.cpp)
  set(MAS_TEST_LIBS mas_clean_core GTest::gtest_main)
  if(NOT MAS_BENCH_ONLY)
    list(APPEND MAS_TEST_SOURCES
      tests/test_pipeline.cpp
      tests/test_duckdb_smoke.cpp
      tests/test_duckdb_event_store.cpp)
    list(APPEND MAS_TEST_LIBS mas_core duckdb_imported)
  endif()
  if(MAS_ENABLE_ZMQ)
    list(APPEND MAS_TEST_SOURCES
      tests/test_zmq_smoke.cpp
      tests/test_message.cpp
      tests/test_zmq_transport.cpp
      tests/test_cleaning_worker.cpp
      tests/test_coordinator.cpp)
    list(APPEND MAS_TEST_LIBS mas_transport cppzmq)
  endif()
  add_executable(unit_tests ${MAS_TEST_SOURCES})
  target_link_libraries(unit_tests PRIVATE ${MAS_TEST_LIBS})
  target_include_directories(unit_tests PRIVATE tests)
  include(GoogleTest)
  gtest_discover_tests(unit_tests)
endif()
```

Replace the `BUILD_RPATH` line with a guarded version — it is meaningless on Windows, and the DLL copy in Task 8 replaces it there:

```cmake
if(NOT MAS_BENCH_ONLY AND NOT WIN32)
  set_target_properties(unit_tests clean_exe merge_exe monolith_exe PROPERTIES
    BUILD_RPATH "${duckdb_prebuilt_SOURCE_DIR}")
  if(MAS_ENABLE_ZMQ)
    set_target_properties(worker_exe coordinator_exe PROPERTIES
      BUILD_RPATH "${duckdb_prebuilt_SOURCE_DIR}")
  endif()
endif()
```

- [ ] **Step 4: Add the Windows DuckDB branch (spec §7.2)**

Only the full build needs this; `MAS_BENCH_ONLY` never downloads DuckDB. Replace
the `if(APPLE) ... else() ... endif()` asset selection (lines 39-50) with a
three-way choice. The current `else()` hard-codes the Linux asset, which would
silently fetch a `.so` on Windows and fail at link time:

```cmake
if(WIN32)
  set(DUCKDB_ASSET libduckdb-windows-amd64.zip)
  set(DUCKDB_LIBNAME duckdb.dll)
  set(DUCKDB_IMPLIB duckdb.lib)        # MSVC links against the import lib,
                                       # not the DLL; ELF and Mach-O need no
                                       # equivalent, which is why the other
                                       # two branches do not set it.
  set(DUCKDB_SHA256 "")                # fill in after the first verified download
elseif(APPLE)
  set(DUCKDB_ASSET libduckdb-osx-universal.zip)
  set(DUCKDB_LIBNAME libduckdb.dylib)
  set(DUCKDB_SHA256 150588828afd4e84cc7b13f69ba62247147a20c814a8b9a21efa55531d2fd3c9)
else()
  set(DUCKDB_ASSET libduckdb-linux-amd64.zip)
  set(DUCKDB_LIBNAME libduckdb.so)
  set(DUCKDB_SHA256 "")                # fill in after the first verified download
endif()
```

Then extend the imported-target block (lines 61-64):

```cmake
add_library(duckdb_imported SHARED IMPORTED GLOBAL)
set_target_properties(duckdb_imported PROPERTIES
  IMPORTED_LOCATION "${duckdb_prebuilt_SOURCE_DIR}/${DUCKDB_LIBNAME}"
  INTERFACE_INCLUDE_DIRECTORIES "${duckdb_prebuilt_SOURCE_DIR}")
if(WIN32)
  set_target_properties(duckdb_imported PROPERTIES
    IMPORTED_IMPLIB "${duckdb_prebuilt_SOURCE_DIR}/${DUCKDB_IMPLIB}")
endif()
```

And add the DLL copy that replaces `BUILD_RPATH` on Windows. Put it after the
executable definitions, inside the `if(NOT MAS_BENCH_ONLY)` block:

```cmake
if(WIN32)
  # No rpath on Windows: the loader looks next to the .exe.
  foreach(tgt clean_exe merge_exe monolith_exe bench_cpu_exe unit_tests)
    if(TARGET ${tgt})
      add_custom_command(TARGET ${tgt} POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
                "${duckdb_prebuilt_SOURCE_DIR}/${DUCKDB_LIBNAME}"
                "$<TARGET_FILE_DIR:${tgt}>")
    endif()
  endforeach()
endif()
```

- [ ] **Step 5: Verify the default build is unchanged**

Run: `rm -rf build && cmake -S . -B build && cmake --build build -j && build/unit_tests 2>&1 | tail -3`
Expected: `[  PASSED  ] 89 tests.` — identical to Task 5. On macOS the `WIN32`
branches are inert, so this also confirms Step 4 did not disturb the Apple path.

- [ ] **Step 6: T9 — the portable-build smoke test**

Run:
```bash
rm -rf build-plan
cmake -S . -B build-plan -DMAS_BENCH_ONLY=ON -DMAS_BUILD_TESTS=OFF
cmake --build build-plan -j
ls build-plan/bench_cpu
find build-plan/_deps -maxdepth 1 -type d 2>/dev/null | grep -Ei 'duckdb|zmq' || echo "NO DUCKDB, NO ZMQ -- portable build is clean"
```
Expected: `bench_cpu` exists, and `NO DUCKDB, NO ZMQ -- portable build is clean`. Nothing downloaded.

- [ ] **Step 7: Verify the tests-on portable build too**

Run: `rm -rf build-plan && cmake -S . -B build-plan -DMAS_BENCH_ONLY=ON && cmake --build build-plan -j && build-plan/unit_tests 2>&1 | tail -3`
Expected: PASS. The count is lower than 88 — DuckDB and ZMQ tests are excluded by design. Record the number; Task 10 documents it.

- [ ] **Step 8: Commit**

```bash
git add CMakeLists.txt
git commit -m "build: MAS_BENCH_ONLY, MAS_ENABLE_ZMQ, MAS_BUILD_TESTS, Windows DuckDB

The benchmark has to configure on a Windows box that has VS Build Tools, CMake,
and the CUDA Toolkit and nothing else. MAS_BENCH_ONLY compiles the whole
comparison with no DuckDB asset and no libzmq build; MAS_BUILD_TESTS=OFF drops
GoogleTest, the last thing that needs network.

ZeroMQ is compiled out, never deleted -- it carries 40 of 73 C++ tests and the
processes-vs-threads axis of the scalability proof.

Default triple is byte-for-byte the old build: 88 tests green."
```

---

## Task 7: `python/clean_vectorized.py`

**Files:**
- Create: `python/clean_vectorized.py`
- Create: `python/tests/test_clean_vectorized.py`
- Create: `bench/requirements-bench.txt`

**Interfaces:**
- Consumes: `oracle.extract` (for the differential test only)
- Produces: `clean_vectorized.extract(path) -> list[tuple]`, `clean_vectorized.EXPECTED_HEADER` — Task 9 imports `extract`

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_clean_vectorized.py`:

```python
"""Differential test: the vectorized cleaner must agree with oracle.py exactly.

oracle.py is the correctness reference the C++ core has been locked against
since Plan 1, so agreeing with it is agreeing with everything.
"""
import glob
import os

import pytest

import clean_vectorized
import oracle

REAL = glob.glob(
    "../telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02/*2026-02-01.csv"
)


def _write_csv(path, rows, eol="\n"):
    """rows: list of (ts, counts[36], torques[36], statuses[36])."""
    with open(path, "w", newline="") as f:
        f.write(",".join(clean_vectorized.EXPECTED_HEADER) + eol)
        for ts, c, t, s in rows:
            f.write(",".join([ts] + [str(x) for x in c + t + s]) + eol)


def _row(ts, count, torque=2.5, status=0.0, n=36):
    return (ts, [count] * n, [torque] * n, [status] * n)


def test_agrees_with_oracle_on_a_synthetic_file(tmp_path):
    p = str(tmp_path / "syn.csv")
    _write_csv(p, [
        _row("2026-02-01T00:00:00.000", 100),          # seed
        _row("2026-02-01T00:00:01.000", 100),          # held
        _row("2026-02-01T00:00:02.000", 101),          # +1
        _row("2026-02-01T00:00:03.000", 106),          # +5, aggregated
        _row("2026-02-01T00:00:04.000", 3),            # reset
        _row("2026-02-01T00:00:05.000", 4, 0.0, 2.0),  # no-load after reset
    ])
    assert clean_vectorized.extract(p) == oracle.extract(p)


def test_agrees_with_oracle_on_reject_statuses(tmp_path):
    p = str(tmp_path / "rej.csv")
    _write_csv(p, [
        _row("2026-02-01T00:00:00.000", 10, 2.0, 0.0),
        _row("2026-02-01T00:00:01.000", 11, 1.9, 65.0),   # Bad Closure, reject
        _row("2026-02-01T00:00:02.000", 12, 1.8, 9.0),    # No InTorque, reject
        _row("2026-02-01T00:00:03.000", 13, 0.5, 4.0),    # No Closure, not a reject
    ])
    assert clean_vectorized.extract(p) == oracle.extract(p)


def test_crlf_yields_the_same_events(tmp_path):
    rows = [_row("2026-02-01T00:00:00.000", 100), _row("2026-02-01T00:00:01.000", 101)]
    lf, crlf = str(tmp_path / "lf.csv"), str(tmp_path / "crlf.csv")
    _write_csv(lf, rows, eol="\n")
    _write_csv(crlf, rows, eol="\r\n")
    assert clean_vectorized.extract(crlf) == clean_vectorized.extract(lf)


def test_rejects_a_wrong_header(tmp_path):
    p = str(tmp_path / "bad.csv")
    with open(p, "w") as f:
        f.write("timestamp,nope,alsonope\n1,2,3\n")
    with pytest.raises(ValueError, match="column 1"):
        clean_vectorized.extract(p)


@pytest.mark.skipif(not REAL, reason="pool not extracted")
def test_agrees_with_oracle_on_a_real_day_file():
    got = clean_vectorized.extract(REAL[0])
    want = oracle.extract(REAL[0])
    assert len(got) == len(want)
    assert got == want
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd python && ../.venv/bin/python -m pytest tests/test_clean_vectorized.py -q 2>&1 | tail -5`
Expected: FAIL — `ModuleNotFoundError: No module named 'clean_vectorized'`

- [ ] **Step 3: Write the implementation**

Create `python/clean_vectorized.py`:

```python
"""Vectorized reference cleaner: same transform as oracle.py, no Python row loop.

oracle.py is the honest naive baseline -- an interpreted loop over 86,399 rows x
36 heads. Comparing that against C++ mostly measures the interpreter. This is the
fair Python contender: pandas parses the CSV, numpy does the transform, and the
only Python-level loop is over the emitted events.

Implements spec 2026-08-10 §3: the transform is element-wise on consecutive row
pairs, so it is one numpy.diff, not a scan.
"""
import sys

import numpy as np
import pandas as pd

NUM_HEADS = 36
FAULT_CODES = {65.0}   # matches oracle.py exactly -- see the note in extract()

EXPECTED_HEADER = (
    ["timestamp"]
    + [f"H{i:02d} Count" for i in range(1, NUM_HEADS + 1)]
    + [f"H{i:02d} AppTorque" for i in range(1, NUM_HEADS + 1)]
    + [f"H{i:02d} Status" for i in range(1, NUM_HEADS + 1)]
)


def _check_header(path):
    with open(path, newline="") as f:
        line = f.readline().rstrip("\r\n")
    got = line.split(",")
    if len(got) != len(EXPECTED_HEADER):
        raise ValueError(
            f"{path}: header has {len(got)} columns, expected {len(EXPECTED_HEADER)}"
        )
    for i, (g, w) in enumerate(zip(got, EXPECTED_HEADER)):
        if g != w:
            raise ValueError(f"{path}: column {i} is {g!r}, expected {w!r}")


def extract(path):
    """Return the same list of 9-tuples oracle.extract returns, same order.

    Tuple shape: (head_id, ts, cap_seq, torque, status, delta, is_fault,
                  aggregated, reset)
    """
    _check_header(path)
    df = pd.read_csv(path, header=0, names=EXPECTED_HEADER, dtype=np.float64,
                     converters={"timestamp": str})
    n_rows = len(df)
    if n_rows < 2:
        return []

    ts = df["timestamp"].to_numpy()
    count = np.rint(df.iloc[:, 1:1 + NUM_HEADS].to_numpy(dtype=np.float64)).astype(np.int64)
    torque = df.iloc[:, 1 + NUM_HEADS:1 + 2 * NUM_HEADS].to_numpy(dtype=np.float64)
    status = df.iloc[:, 1 + 2 * NUM_HEADS:].to_numpy(dtype=np.float64)

    # Spec §3: last_count_[h] after row i is always count[i][h], so the whole
    # transform is a one-row difference. Row 0 is the seed and emits nothing.
    delta = count[1:] - count[:-1]
    rows, heads = np.nonzero(delta)          # nonzero returns row-major order,
                                             # i.e. (row asc, head asc) -- the
                                             # emission order the C++ uses.
    if rows.size == 0:
        return []

    d = delta[rows, heads]
    cur = count[1:][rows, heads]
    tq = torque[1:][rows, heads]
    st = status[1:][rows, heads]
    is_reset = d < 0
    out_delta = np.where(is_reset, 0, d)

    # oracle.py tests `status in FAULT_CODES` (i.e. == 65.0), NOT the C++
    # is_reject() bitmask. This mirrors oracle.py so the differential test is
    # exact; the C++ tuple is not the comparison target here.
    return [
        (int(heads[k]) + 1, ts[rows[k] + 1], int(cur[k]), float(tq[k]), float(st[k]),
         int(out_delta[k]), bool(st[k] in FAULT_CODES), bool(out_delta[k] > 1),
         bool(is_reset[k]))
        for k in range(rows.size)
    ]


if __name__ == "__main__":
    print(len(extract(sys.argv[1])))
```

Create `bench/requirements-bench.txt`:

```
numpy>=1.24
pandas>=2.0
matplotlib>=3.7
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd python && ../.venv/bin/python -m pytest tests/test_clean_vectorized.py -q 2>&1 | tail -5`
Expected: `5 passed` (or `4 passed, 1 skipped` if the pool is not extracted — extract it and re-run; the real-data test must run).

- [ ] **Step 5: Verify against the C++ count end to end**

Run:
```bash
D=telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02
cd python && ../.venv/bin/python clean_vectorized.py ../$D/telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02-01.csv
```
Expected: `765711` — matching `bench_cpu` from Task 4 Step 4.

- [ ] **Step 6: Confirm no regression**

Run: `.venv/bin/python -m pytest python/tests -q 2>&1 | tail -3`
Expected: `224 passed, 5 skipped` (219 + 5 new).

- [ ] **Step 7: Commit**

```bash
git add python/clean_vectorized.py python/tests/test_clean_vectorized.py bench/requirements-bench.txt
git commit -m "feat(bench): vectorized Python contender

oracle.py loops in the interpreter over 86,399 rows x 36 heads. Benchmarking
that against C++ measures the interpreter, not the language gap, so the
comparison needs a Python entry that a Python developer would actually write.

The element-wise finding does the work here too: the transform is one
numpy.diff, and np.nonzero already returns (row asc, head asc), which is the
C++ emission order."
```

---

## Task 8: The CUDA pipeline

**Cannot be compiled or run on the development machine.** Write it, keep it conventional, and let the driver verify it on the target box. Spec §10 R3.

**Files:**
- Create: `core/cuda/CudaCleaner.hpp`
- Create: `core/cuda/CudaCleaner.cu`
- Create: `core/src/apps/cuda_clean_main.cpp`
- Modify: `CMakeLists.txt` (CUDA block)

**Interfaces:**
- Consumes: `mas::CapEvent`, `mas::NUM_HEADS`, `mas::is_reject`, `mas::expected_header`, `mas::read_metrics`, `mas::metrics_line`
- Produces: `mas::CudaStageTimes`, `mas::cuda_clean_file(path, events, times, error) -> bool`; binary `mas_cuda_clean`

- [ ] **Step 1: Write the interface header**

Create `core/cuda/CudaCleaner.hpp`. No CUDA types appear here, so `cuda_clean_main.cpp` compiles as plain C++:

```cpp
#pragma once
#include "mas/domain/CapEvent.hpp"
#include <string>
#include <vector>

namespace mas {

// Per-stage GPU timings, seconds. Spec §6.4: this breakdown is the point --
// it says how much of the win is "GPU parses the CSV" versus "GPU does the
// compare", which is what justifies porting the transform at all.
struct CudaStageTimes {
    double read_s = 0, h2d_s = 0, index_s = 0, parse_s = 0,
           delta_s = 0, compact_s = 0, d2h_s = 0;
};

// Clean one raw telemetry day-file on the GPU. Appends events in
// (row asc, head asc) order -- identical to CapEventExtractor's. Returns false
// and fills `error` on any failure.
bool cuda_clean_file(const std::string& path, std::vector<CapEvent>& out,
                     CudaStageTimes& times, std::string& error);

} // namespace mas
```

- [ ] **Step 2: Write the kernels**

Create `core/cuda/CudaCleaner.cu`:

```cu
#include "CudaCleaner.hpp"
#include "mas/domain/CapEventExtractorFlat.hpp"   // expected_header()
#include <cub/cub.cuh>
#include <cuda_runtime.h>
#include <cstdio>
#include <fstream>
#include <string>
#include <vector>

namespace mas {
namespace {

constexpr int TS_LEN = 24;      // "2026-02-21T16:00:00.000" is 23 chars + NUL

// Device-side event slot. 40 bytes: torque and status stay double so the
// differential test against CapEventExtractorFlat can compare bitwise rather
// than with a tolerance (spec §9 T3). is_fault and aggregated are derived on
// the host from status and delta -- no reason to spend device bandwidth on them.
struct CapEventDevice {
    long long cap_seq;
    double app_torque;
    double status;
    unsigned int row_index;
    int delta;
    short head_id;
    unsigned char reset;
    unsigned char pad[5];
};

#define CUDA_TRY(expr, what)                                              \
    do {                                                                  \
        const cudaError_t rc_ = (expr);                                   \
        if (rc_ != cudaSuccess) {                                         \
            error = std::string(what) + ": " + cudaGetErrorString(rc_);   \
            return false;                                                 \
        }                                                                 \
    } while (0)

__device__ __forceinline__ double pow10_(int e) {
    double r = 1.0;
    for (int i = 0; i < e; ++i) r *= 10.0;   // exact in double for e <= 22
    return r;
}

// Integer-mantissa parse: accumulate digits as an integer, divide once by an
// exact power of ten. Correctly rounded for the <=3 decimal places this pool
// uses, unlike repeated fused multiply-add. Stops at any non-digit, which is
// how a trailing '\r' is absorbed for free.
__device__ __forceinline__ double parse_num(const char* p, const char* e) {
    bool neg = false;
    if (p < e && (*p == '-' || *p == '+')) { neg = (*p == '-'); ++p; }
    long long mant = 0;
    int scale = 0;
    bool frac = false;
    for (; p < e; ++p) {
        const char c = *p;
        if (c == '.') { frac = true; continue; }
        if (c < '0' || c > '9') break;
        mant = mant * 10 + (c - '0');
        if (frac) ++scale;
    }
    double v = static_cast<double>(mant);
    if (scale > 0) v /= pow10_(scale);
    return neg ? -v : v;
}

__global__ void flag_newlines(const char* buf, size_t n, unsigned char* flags) {
    const size_t i = blockIdx.x * static_cast<size_t>(blockDim.x) + threadIdx.x;
    if (i < n) flags[i] = (buf[i] == '\n') ? 1u : 0u;
}

// One thread per data row. Uncoalesced by construction -- each thread walks its
// own ~650 contiguous bytes -- but the whole file is 58 MB, so even a large
// efficiency loss lands in single-digit milliseconds. Spec §5.5.
__global__ void parse_rows(const char* buf, const unsigned long long* nl,
                           unsigned int n_rows, double* count, double* torque,
                           double* status, char* ts_out) {
    const unsigned int r = blockIdx.x * blockDim.x + threadIdx.x;
    if (r >= n_rows) return;

    // nl[k] is the offset of the k-th '\n'. nl[0] ends the header, so data row r
    // spans (nl[r], nl[r + 1]).
    const char* p = buf + nl[r] + 1;
    const char* e = buf + nl[r + 1];
    while (e > p && (e[-1] == '\r' || e[-1] == '\n')) --e;

    const char* q = p;
    while (q < e && *q != ',') ++q;
    const int tslen = static_cast<int>(q - p);
    for (int i = 0; i < TS_LEN; ++i)
        ts_out[static_cast<size_t>(r) * TS_LEN + i] = (i < tslen && i < TS_LEN - 1) ? p[i] : '\0';
    p = (q < e) ? q + 1 : e;

    for (int f = 0; f < 3 * NUM_HEADS; ++f) {
        q = p;
        while (q < e && *q != ',') ++q;
        const double v = parse_num(p, q);
        const int h = f % NUM_HEADS;
        const size_t idx = static_cast<size_t>(r) * NUM_HEADS + h;
        if (f < NUM_HEADS)            count[idx] = v;
        else if (f < 2 * NUM_HEADS)   torque[idx] = v;
        else                          status[idx] = v;
        p = (q < e) ? q + 1 : e;
    }
}

// One thread per (row, head) for rows 1..n_rows-1. Row 0 is the seed.
__global__ void delta_kernel(const double* count, const double* torque,
                             const double* status, unsigned int n_rows,
                             CapEventDevice* slots, unsigned char* flags) {
    const size_t t = blockIdx.x * static_cast<size_t>(blockDim.x) + threadIdx.x;
    const size_t total = static_cast<size_t>(n_rows - 1) * NUM_HEADS;
    if (t >= total) return;

    const unsigned int i = static_cast<unsigned int>(t / NUM_HEADS) + 1;
    const int h = static_cast<int>(t % NUM_HEADS);
    const size_t cur = static_cast<size_t>(i) * NUM_HEADS + h;
    const size_t prv = static_cast<size_t>(i - 1) * NUM_HEADS + h;

    const long long c_cur = llround(count[cur]);
    const long long c_prv = llround(count[prv]);
    if (c_cur == c_prv) { flags[t] = 0; return; }

    CapEventDevice ev;
    ev.cap_seq = c_cur;
    ev.app_torque = torque[cur];
    ev.status = status[cur];
    ev.row_index = i;
    ev.delta = (c_cur > c_prv) ? static_cast<int>(c_cur - c_prv) : 0;
    ev.head_id = static_cast<short>(h + 1);
    ev.reset = (c_cur < c_prv) ? 1u : 0u;
    slots[t] = ev;
    flags[t] = 1;
}

struct Timer {
    cudaEvent_t a{}, b{};
    Timer()  { cudaEventCreate(&a); cudaEventCreate(&b); }
    ~Timer() { cudaEventDestroy(a); cudaEventDestroy(b); }
    void start() { cudaEventRecord(a); }
    double stop() {
        cudaEventRecord(b);
        cudaEventSynchronize(b);
        float ms = 0;
        cudaEventElapsedTime(&ms, a, b);
        return ms * 1e-3;
    }
};

bool check_header(const std::string& path, std::string& error) {
    std::ifstream in(path, std::ios::binary);
    if (!in.is_open()) { error = "cannot open " + path; return false; }
    std::string line;
    if (!std::getline(in, line)) { error = path + " is empty"; return false; }
    if (!line.empty() && line.back() == '\r') line.pop_back();
    const auto want = expected_header();
    std::vector<std::string> got;
    std::string cur;
    for (char c : line) {
        if (c == ',') { got.push_back(cur); cur.clear(); }
        else cur.push_back(c);
    }
    got.push_back(cur);
    if (got.size() != want.size()) {
        error = path + ": header has " + std::to_string(got.size()) +
                " columns, expected " + std::to_string(want.size());
        return false;
    }
    for (size_t i = 0; i < want.size(); ++i)
        if (got[i] != want[i]) {
            error = path + ": column " + std::to_string(i) + " is '" + got[i] +
                    "', expected '" + want[i] + "'";
            return false;
        }
    return true;
}

} // namespace

bool cuda_clean_file(const std::string& path, std::vector<CapEvent>& out,
                     CudaStageTimes& times, std::string& error) {
    if (!check_header(path, error)) return false;

    // ---- S0: read into pinned host memory -----------------------------------
    // No mmap: it is POSIX-only and would need a CreateFileMapping branch for
    // Windows. At 58 MB a plain binary read costs the same (spec §5.5).
    std::ifstream in(path, std::ios::binary | std::ios::ate);
    if (!in.is_open()) { error = "cannot open " + path; return false; }
    const size_t n_bytes = static_cast<size_t>(in.tellg());
    in.seekg(0);

    char* h_buf = nullptr;
    CUDA_TRY(cudaHostAlloc(&h_buf, n_bytes, cudaHostAllocDefault), "cudaHostAlloc");
    Timer t;
    t.start();
    in.read(h_buf, static_cast<std::streamsize>(n_bytes));
    times.read_s = t.stop();

    char* d_buf = nullptr;
    unsigned char* d_flags = nullptr;
    unsigned long long* d_nl = nullptr;
    unsigned int* d_nl_count = nullptr;
    double *d_count = nullptr, *d_torque = nullptr, *d_status = nullptr;
    char* d_ts = nullptr;
    CapEventDevice *d_slots = nullptr, *d_dense = nullptr;
    unsigned char* d_evflags = nullptr;
    unsigned int* d_ev_count = nullptr;
    void* d_tmp = nullptr;
    auto cleanup = [&] {
        cudaFree(d_buf); cudaFree(d_flags); cudaFree(d_nl); cudaFree(d_nl_count);
        cudaFree(d_count); cudaFree(d_torque); cudaFree(d_status); cudaFree(d_ts);
        cudaFree(d_slots); cudaFree(d_dense); cudaFree(d_evflags);
        cudaFree(d_ev_count); cudaFree(d_tmp); cudaFreeHost(h_buf);
    };
#define FAIL_IF(expr, what)                                               \
    do { const cudaError_t rc_ = (expr);                                  \
         if (rc_ != cudaSuccess) {                                        \
             error = std::string(what) + ": " + cudaGetErrorString(rc_);  \
             cleanup(); return false; } } while (0)

    // ---- S1: upload ---------------------------------------------------------
    FAIL_IF(cudaMalloc(&d_buf, n_bytes), "cudaMalloc raw");
    t.start();
    FAIL_IF(cudaMemcpy(d_buf, h_buf, n_bytes, cudaMemcpyHostToDevice), "H2D raw");
    times.h2d_s = t.stop();

    // ---- S2: newline index --------------------------------------------------
    FAIL_IF(cudaMalloc(&d_flags, n_bytes), "cudaMalloc flags");
    FAIL_IF(cudaMalloc(&d_nl, n_bytes / 8 + 2), "cudaMalloc nl");   // >= one per line
    FAIL_IF(cudaMalloc(&d_nl_count, sizeof(unsigned int)), "cudaMalloc nl_count");
    t.start();
    {
        const int blk = 256;
        const size_t grid = (n_bytes + blk - 1) / blk;
        flag_newlines<<<static_cast<unsigned int>(grid), blk>>>(d_buf, n_bytes, d_flags);
        cub::CountingInputIterator<unsigned long long> idx(0);
        size_t tmp_bytes = 0;
        cub::DeviceSelect::Flagged(nullptr, tmp_bytes, idx, d_flags, d_nl,
                                   d_nl_count, static_cast<int>(n_bytes));
        FAIL_IF(cudaMalloc(&d_tmp, tmp_bytes), "cudaMalloc cub tmp");
        cub::DeviceSelect::Flagged(d_tmp, tmp_bytes, idx, d_flags, d_nl,
                                   d_nl_count, static_cast<int>(n_bytes));
    }
    times.index_s = t.stop();
    FAIL_IF(cudaGetLastError(), "S2 index");

    unsigned int n_lines = 0;
    FAIL_IF(cudaMemcpy(&n_lines, d_nl_count, sizeof(unsigned int),
                       cudaMemcpyDeviceToHost), "D2H nl_count");
    if (n_lines < 2) { cleanup(); out.clear(); return true; }   // header only
    const unsigned int n_rows = n_lines - 1;                    // data rows

    // ---- S3: parse ----------------------------------------------------------
    const size_t cells = static_cast<size_t>(n_rows) * NUM_HEADS;
    FAIL_IF(cudaMalloc(&d_count, cells * sizeof(double)), "cudaMalloc count");
    FAIL_IF(cudaMalloc(&d_torque, cells * sizeof(double)), "cudaMalloc torque");
    FAIL_IF(cudaMalloc(&d_status, cells * sizeof(double)), "cudaMalloc status");
    FAIL_IF(cudaMalloc(&d_ts, static_cast<size_t>(n_rows) * TS_LEN), "cudaMalloc ts");
    t.start();
    {
        const int blk = 128;
        const unsigned int grid = (n_rows + blk - 1) / blk;
        parse_rows<<<grid, blk>>>(d_buf, d_nl, n_rows, d_count, d_torque, d_status, d_ts);
    }
    times.parse_s = t.stop();
    FAIL_IF(cudaGetLastError(), "S3 parse");

    // ---- S4: delta ----------------------------------------------------------
    const size_t slots = static_cast<size_t>(n_rows - 1) * NUM_HEADS;
    FAIL_IF(cudaMalloc(&d_slots, slots * sizeof(CapEventDevice)), "cudaMalloc slots");
    FAIL_IF(cudaMalloc(&d_evflags, slots), "cudaMalloc evflags");
    t.start();
    {
        const int blk = 256;
        const unsigned int grid = static_cast<unsigned int>((slots + blk - 1) / blk);
        delta_kernel<<<grid, blk>>>(d_count, d_torque, d_status, n_rows, d_slots, d_evflags);
    }
    times.delta_s = t.stop();
    FAIL_IF(cudaGetLastError(), "S4 delta");

    // ---- S5: compact --------------------------------------------------------
    FAIL_IF(cudaMalloc(&d_dense, slots * sizeof(CapEventDevice)), "cudaMalloc dense");
    FAIL_IF(cudaMalloc(&d_ev_count, sizeof(unsigned int)), "cudaMalloc ev_count");
    t.start();
    {
        void* tmp2 = nullptr;
        size_t tmp2_bytes = 0;
        cub::DeviceSelect::Flagged(nullptr, tmp2_bytes, d_slots, d_evflags, d_dense,
                                   d_ev_count, static_cast<int>(slots));
        FAIL_IF(cudaMalloc(&tmp2, tmp2_bytes), "cudaMalloc cub tmp2");
        cub::DeviceSelect::Flagged(tmp2, tmp2_bytes, d_slots, d_evflags, d_dense,
                                   d_ev_count, static_cast<int>(slots));
        cudaFree(tmp2);
    }
    times.compact_s = t.stop();
    FAIL_IF(cudaGetLastError(), "S5 compact");

    unsigned int n_events = 0;
    FAIL_IF(cudaMemcpy(&n_events, d_ev_count, sizeof(unsigned int),
                       cudaMemcpyDeviceToHost), "D2H ev_count");

    // ---- S6: download -------------------------------------------------------
    std::vector<CapEventDevice> host_ev(n_events);
    std::vector<char> host_ts(static_cast<size_t>(n_rows) * TS_LEN);
    t.start();
    if (n_events)
        FAIL_IF(cudaMemcpy(host_ev.data(), d_dense, n_events * sizeof(CapEventDevice),
                           cudaMemcpyDeviceToHost), "D2H events");
    FAIL_IF(cudaMemcpy(host_ts.data(), d_ts, host_ts.size(),
                       cudaMemcpyDeviceToHost), "D2H ts");
    times.d2h_s = t.stop();

    // ---- host: materialize --------------------------------------------------
    out.reserve(out.size() + n_events);
    for (unsigned int k = 0; k < n_events; ++k) {
        const CapEventDevice& d = host_ev[k];
        CapEvent e;
        e.head_id = d.head_id;
        e.ts = std::string(&host_ts[static_cast<size_t>(d.row_index) * TS_LEN]);
        e.cap_seq = d.cap_seq;
        e.app_torque = d.app_torque;
        e.status = d.status;
        e.delta = d.delta;
        e.is_fault = is_reject(d.status);
        e.aggregated = d.delta > 1;
        e.reset = d.reset != 0;
        out.push_back(e);
    }
#undef FAIL_IF
    cleanup();
    return true;
}

} // namespace mas
```

- [ ] **Step 3: Write the CLI**

Create `core/src/apps/cuda_clean_main.cpp`. It carries T3 — the differential check against `CapEventExtractorFlat` — behind a `--verify` flag, so the driver can assert correctness without a second binary:

```cpp
#include "../../cuda/CudaCleaner.hpp"
#include "mas/domain/CapEventExtractorFlat.hpp"
#include "mas/util/platform_metrics.hpp"
#include <cstring>
#include <iomanip>
#include <iostream>
#include <string>
#include <vector>

namespace {

// Bitwise on the doubles, not EXPECT_NEAR: a GPU parse that is one ulp off is a
// bug to find now, not a tolerance to widen (spec §10 R2).
bool sameEvent(const mas::CapEvent& a, const mas::CapEvent& b) {
    return a.head_id == b.head_id && a.ts == b.ts && a.cap_seq == b.cap_seq &&
           std::memcmp(&a.app_torque, &b.app_torque, sizeof(double)) == 0 &&
           std::memcmp(&a.status, &b.status, sizeof(double)) == 0 &&
           a.delta == b.delta && a.is_fault == b.is_fault &&
           a.aggregated == b.aggregated && a.reset == b.reset;
}

void dump(const char* which, const mas::CapEvent& e) {
    std::cerr << "    " << which << ": head=" << e.head_id << " ts=" << e.ts
              << " cap_seq=" << e.cap_seq << " torque=" << std::setprecision(17)
              << e.app_torque << " status=" << e.status << " delta=" << e.delta
              << " fault=" << e.is_fault << " agg=" << e.aggregated
              << " reset=" << e.reset << "\n";
}

// Spec §8: a failure on a machine the author cannot reach must be diagnosable
// from one paste. Ten events with every field is that artifact.
int reportMismatch(const std::vector<mas::CapEvent>& cpu,
                   const std::vector<mas::CapEvent>& gpu) {
    std::cerr << "VERIFY FAILED: cpu " << cpu.size() << " events, gpu "
              << gpu.size() << " events\n";
    int shown = 0;
    const std::size_t n = std::min(cpu.size(), gpu.size());
    for (std::size_t i = 0; i < n && shown < 10; ++i) {
        if (sameEvent(cpu[i], gpu[i])) continue;
        std::cerr << "  index " << i << ":\n";
        dump("cpu", cpu[i]);
        dump("gpu", gpu[i]);
        ++shown;
    }
    if (shown == 0) std::cerr << "  events agree; only the counts differ\n";
    return 1;
}

} // namespace

int main(int argc, char** argv) {
    mas::metrics_init();
    bool verify = false;
    int argi = 1;
    while (argi < argc && std::string(argv[argi]).rfind("--", 0) == 0) {
        if (std::string(argv[argi]) == "--verify") verify = true;
        else { std::cerr << "unknown flag " << argv[argi] << "\n"; return 2; }
        ++argi;
    }
    if (argi >= argc) {
        std::cerr << "usage: mas_cuda_clean [--verify] <day1.csv> [day2.csv ...]\n";
        return 2;
    }

    long long events = 0;
    mas::CudaStageTimes total;
    for (int i = argi; i < argc; ++i) {
        std::vector<mas::CapEvent> gpu;
        mas::CudaStageTimes t;
        std::string error;
        if (!mas::cuda_clean_file(argv[i], gpu, t, error)) {
            std::cerr << "error: " << error << "\n";
            return 1;
        }
        total.read_s += t.read_s;   total.h2d_s += t.h2d_s;
        total.index_s += t.index_s; total.parse_s += t.parse_s;
        total.delta_s += t.delta_s; total.compact_s += t.compact_s;
        total.d2h_s += t.d2h_s;
        events += static_cast<long long>(gpu.size());

        if (verify) {
            mas::RawColumns cols;
            std::string err;
            if (!mas::load_columns(argv[i], cols, err)) {
                std::cerr << "error: " << err << "\n";
                return 1;
            }
            std::vector<mas::CapEvent> cpu;
            mas::extract_flat(cols.ts, cols.count.data(), cols.torque.data(),
                              cols.status.data(), cols.n_rows, cpu);
            if (cpu.size() != gpu.size()) return reportMismatch(cpu, gpu);
            for (std::size_t k = 0; k < cpu.size(); ++k)
                if (!sameEvent(cpu[k], gpu[k])) return reportMismatch(cpu, gpu);
            std::cerr << "verify ok: " << argv[i] << " (" << cpu.size() << " events)\n";
        }
    }

    const double clean_s = total.read_s + total.h2d_s + total.index_s +
                           total.parse_s + total.delta_s + total.compact_s + total.d2h_s;
    std::cerr << "cuda_clean: " << (argc - argi) << " files, " << events
              << " events, clean " << std::fixed << std::setprecision(3)
              << clean_s << " s\n";
    std::cerr << "stages: read_s=" << total.read_s << " h2d_s=" << total.h2d_s
              << " index_s=" << total.index_s << " parse_s=" << total.parse_s
              << " delta_s=" << total.delta_s << " compact_s=" << total.compact_s
              << " d2h_s=" << total.d2h_s << "\n";
    std::cerr << mas::metrics_line("clean", mas::read_metrics()) << "\n";
    return 0;
}
```

- [ ] **Step 4: Add the CUDA block to CMake**

In `CMakeLists.txt`, after the `bench_cpu_exe` block:

```cmake
if(MAS_ENABLE_CUDA)
  find_package(CUDAToolkit REQUIRED)
  enable_language(CUDA)
  add_executable(cuda_clean_exe core/cuda/CudaCleaner.cu core/src/apps/cuda_clean_main.cpp)
  target_link_libraries(cuda_clean_exe PRIVATE mas_clean_core CUDA::cudart)
  target_include_directories(cuda_clean_exe PRIVATE core/cuda)
  set_target_properties(cuda_clean_exe PROPERTIES OUTPUT_NAME mas_cuda_clean)
  # CUDA_ARCHITECTURES native needs CMake >= 3.24 and the target machine's
  # version is unknown, so fall back to an explicit list covering Pascal..Ada.
  if(CMAKE_VERSION VERSION_GREATER_EQUAL 3.24)
    set_target_properties(cuda_clean_exe PROPERTIES CUDA_ARCHITECTURES native)
  else()
    set_target_properties(cuda_clean_exe PROPERTIES CUDA_ARCHITECTURES "60;70;75;80;86;89")
  endif()
endif()
```

- [ ] **Step 5: Verify it does not break the non-CUDA build**

Run: `rm -rf build-plan && cmake -S . -B build-plan -DMAS_BENCH_ONLY=ON && cmake --build build-plan -j && build-plan/unit_tests 2>&1 | tail -3`
Expected: PASS. `MAS_ENABLE_CUDA` defaults OFF, so no `.cu` file is compiled and no CUDA language is enabled. **This is the only verification possible on the development machine** — the kernels themselves are checked by the driver on the target box (Task 9, Step 5).

- [ ] **Step 6: Commit**

```bash
git add core/cuda/CudaCleaner.hpp core/cuda/CudaCleaner.cu \
        core/src/apps/cuda_clean_main.cpp CMakeLists.txt
git commit -m "feat(cuda): GPU cleaning pipeline

Seven stages: read into pinned memory, one upload of the raw bytes, CUB newline
index, thread-per-row parse, thread-per-(row,head) delta, CUB compaction, one
download of the dense events. The delta kernel is the payoff of the element-wise
finding -- 3.1M threads doing two loads and a compare.

Timestamps never reach the GPU as strings: events carry a row index and the host
maps it back, which is why the device struct stays flat.

Torque and status stay double so --verify can compare bitwise against
CapEventExtractorFlat. A parse that is one ulp out is a bug to find, not a
tolerance to widen.

Untested: this machine is an M3 with no nvcc. The verify path and the ten-event
mismatch dump exist so the first run on real hardware is diagnosable from one
paste."
```

---

## Task 9: The benchmark driver

**Files:**
- Create: `bench/run_bench_cuda.py`
- Create: `bench/README.md`

**Interfaces:**
- Consumes: `bench_cpu`, `mas_cuda_clean`, `mas_monolith` binaries; `python/oracle.py`, `python/oracle_union.py`, `python/clean_vectorized.py`
- Produces: `bench/results_cuda.csv`, `bench/results_cuda_stages.csv`

- [ ] **Step 1: Write the driver**

Create `bench/run_bench_cuda.py`:

```python
#!/usr/bin/env python3
"""Three-way cleaning benchmark: Python, C++, CUDA, one machine, one command.

Replaces bench/run_bench.sh for this sweep. That script needs bash,
/usr/bin/time -l, unzip, find, sed and awk, and works around macOS bash 3.2 --
none of which exist on the Windows box this is meant to run on. Python is
already a hard requirement here, since it is two of the contenders.

Usage:
    python bench/run_bench_cuda.py --data <month.zip | extracted_dir>
    python bench/run_bench_cuda.py --data ... --quick     # 1-day volume only
"""
import argparse
import csv
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY_DIR = os.path.join(ROOT, "python")
ROWS_PER_DAY = 86399
REPEATS = 3
VOLUMES = (1, 7, 28)
PY_NAIVE_MAX_FILES = 1        # spec §6.3: 28 days is ~2 h for the naive path

RESULTS = os.path.join(ROOT, "bench", "results_cuda.csv")
STAGES = os.path.join(ROOT, "bench", "results_cuda_stages.csv")

FIELDS = ["arch", "mode", "n_workers", "threads", "files", "repeat", "clean_s",
          "merge_s", "total_s", "events", "rows_per_s", "events_per_s",
          "peak_rss_mb", "cpu_pct", "note"]
STAGE_FIELDS = ["files", "repeat", "read_s", "h2d_s", "index_s", "parse_s",
                "delta_s", "compact_s", "d2h_s", "store_s"]

_METRICS = re.compile(
    r"metrics: tag=(\S+) wall_s=([\d.]+) cpu_s=([\d.]+) peak_rss_mb=([\d.]+)")
_EVENTS = re.compile(r"(\d+) events")
_CLEAN = re.compile(r"clean ([\d.]+) s")
_STAGE = re.compile(r"stages: (.*)")


def die(msg, fix=None):
    print(f"\nERROR: {msg}", file=sys.stderr)
    if fix:
        print(f"  fix: {fix}", file=sys.stderr)
    sys.exit(1)


def find_binary(name):
    """MSVC multi-config puts binaries in build/Release; make both work."""
    exe = name + (".exe" if os.name == "nt" else "")
    for d in ("build", "build/Release", "build-plan", "build-plan/Release"):
        p = os.path.join(ROOT, d, exe)
        if os.path.isfile(p):
            return p
    return None


def extract_pool(data):
    """Return a sorted list of day-file paths, extracting the zip if needed."""
    if os.path.isdir(data):
        out_dir = data
    else:
        if not os.path.isfile(data):
            die(f"{data} is neither a directory nor a file")
        out_dir = os.path.splitext(data)[0]
        free_mb = shutil.disk_usage(ROOT).free // (1024 * 1024)
        if free_mb < 2048 and not os.path.isdir(out_dir):
            die(f"only {free_mb} MB free; extraction needs about 2 GB",
                "free some disk and retry")
        os.makedirs(out_dir, exist_ok=True)
        with zipfile.ZipFile(data) as z:
            members = [m for m in z.namelist() if m.endswith(".csv")]
            for m in members:
                target = os.path.join(out_dir, os.path.basename(m))
                if not os.path.exists(target):
                    with z.open(m) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
    files = sorted(
        os.path.join(out_dir, f) for f in os.listdir(out_dir) if f.endswith(".csv"))
    if not files:
        die(f"no CSV files under {out_dir}")
    return files


def provenance():
    lines = [
        f"# platform: {platform.platform()}",
        f"# processor: {platform.processor() or 'unknown'}",
        f"# python: {sys.version.split()[0]}",
    ]
    try:
        gpu = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=30)
        lines.append(f"# gpu: {gpu.stdout.strip() or 'none'}")
    except (OSError, subprocess.SubprocessError):
        lines.append("# gpu: nvidia-smi not found")
    try:
        nv = subprocess.run(["nvcc", "--version"], capture_output=True,
                            text=True, timeout=30)
        tail = nv.stdout.strip().splitlines()[-1] if nv.stdout.strip() else "unknown"
        lines.append(f"# nvcc: {tail}")
    except (OSError, subprocess.SubprocessError):
        lines.append("# nvcc: not found")
    return lines


def run(cmd, cwd=None):
    """Run and return (stdout+stderr, wall_s, cpu_s, peak_rss_mb, events, clean_s)."""
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    blob = p.stdout + p.stderr
    if p.returncode != 0:
        die(f"{' '.join(str(c) for c in cmd)} exited {p.returncode}\n{blob}")
    m = _METRICS.search(blob)
    wall, cpu, rss = (float(m.group(2)), float(m.group(3)), float(m.group(4))) \
        if m else (0.0, 0.0, 0.0)
    ev = _EVENTS.search(blob)
    cl = _CLEAN.search(blob)
    events = int(ev.group(1)) if ev else 0
    clean_s = float(cl.group(1)) if cl else wall
    return blob, wall, cpu, rss, events, clean_s


def oracle_union(files):
    out = subprocess.run(
        [sys.executable, "oracle_union.py"] + [os.path.relpath(f, PY_DIR) for f in files],
        cwd=PY_DIR, capture_output=True, text=True)
    if out.returncode != 0:
        die(f"oracle_union.py failed:\n{out.stdout}{out.stderr}")
    return int(out.stdout.strip())


def emit(writer, arch, mode, threads, nfiles, rep, clean_s, total_s, events,
         rss, cpu_s, note=""):
    writer.writerow({
        "arch": arch, "mode": mode, "n_workers": 0, "threads": threads,
        "files": nfiles, "repeat": rep,
        "clean_s": f"{clean_s:.3f}", "merge_s": "0.000",
        "total_s": f"{total_s:.3f}", "events": events,
        "rows_per_s": f"{ROWS_PER_DAY * nfiles / total_s:.1f}" if total_s else "0",
        "events_per_s": f"{events / total_s:.1f}" if total_s else "0",
        "peak_rss_mb": f"{rss:.1f}",
        "cpu_pct": f"{100.0 * cpu_s / total_s:.1f}" if total_s else "0",
        "note": note,
    })


def py_arch_time(module, files):
    """Time a Python cleaner in-process-per-file, summing events."""
    script = (
        "import sys, time; sys.path.insert(0, '.');"
        f"import {module} as m;"
        "t=time.perf_counter(); n=0\n"
        "for p in sys.argv[1:]: n += len(m.extract(p))\n"
        "print(f'{n} events'); print(f'clean {time.perf_counter()-t:.3f} s')"
    )
    p = subprocess.run([sys.executable, "-c", script] + files,
                       cwd=PY_DIR, capture_output=True, text=True)
    if p.returncode != 0:
        die(f"{module} failed:\n{p.stdout}{p.stderr}")
    blob = p.stdout + p.stderr
    return int(_EVENTS.search(blob).group(1)), float(_CLEAN.search(blob).group(1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="month zip or extracted directory")
    ap.add_argument("--quick", action="store_true", help="1-day volume only")
    args = ap.parse_args()

    files = extract_pool(args.data)
    volumes = (1,) if args.quick else tuple(v for v in VOLUMES if v <= len(files))

    bench_cpu = find_binary("bench_cpu")
    cuda = find_binary("mas_cuda_clean")
    mono = find_binary("mas_monolith")
    if not bench_cpu:
        die("bench_cpu not found",
            "cmake -S . -B build -DMAS_BENCH_ONLY=ON -DMAS_ENABLE_CUDA=ON && "
            "cmake --build build --config Release")
    try:
        import numpy, pandas   # noqa: F401
    except ImportError:
        die("numpy and pandas are required",
            "pip install -r bench/requirements-bench.txt")
    if not cuda:
        print("WARNING: mas_cuda_clean not found -- CUDA rows will be missing. "
              "Configure with -DMAS_ENABLE_CUDA=ON to include them.\n")

    # Reference only, and only meaningful for `e2e`: this is UNIQUE(head,
    # cap_seq) after the counter reset dedupes replayed ranges, i.e. what a
    # *store* ends up holding. Store-free runs emit more than this on
    # multi-day volumes. The `clean` gate is the cross-arch check below.
    print("Store row counts the e2e runs should land on (oracle_union):")
    oracle = {v: oracle_union(files[:v]) for v in volumes}
    for v, n in oracle.items():
        print(f"  {v:2d} day-file(s): {n} rows")

    with open(RESULTS, "w", newline="") as fh:
        for line in provenance():
            fh.write(line + "\n")
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()

        stage_fh = open(STAGES, "w", newline="")
        sw = csv.DictWriter(stage_fh, fieldnames=STAGE_FIELDS)
        sw.writeheader()

        naive_1day = []          # clean_s samples, used for the extrapolation

        for v in volumes:
            sub = files[:v]
            for rep in range(1, REPEATS + 1):
                # Every arch cleaning the same files must emit the same number of
                # events. That cross-check is the correctness gate for `clean`
                # mode: oracle_union counts UNIQUE(head, cap_seq) rows, which is
                # what a *store* holds after the counter reset dedupes replays --
                # not what a store-free run emits. Comparing against it here
                # would fail for the wrong reason on multi-day volumes.
                seen = {}

                # --- Python contenders ---------------------------------------
                for arch, module in (("py-naive", "oracle"),
                                     ("py-numpy", "clean_vectorized")):
                    if arch == "py-naive" and v > PY_NAIVE_MAX_FILES:
                        continue          # spec §6.3; extrapolated below
                    rel = [os.path.relpath(f, PY_DIR) for f in sub]
                    events, clean_s = py_arch_time(module, rel)
                    seen[arch] = events
                    if arch == "py-naive" and v == 1:
                        naive_1day.append(clean_s)
                    emit(w, arch, "clean", 1, v, rep, clean_s, clean_s, events, 0.0, 0.0)
                    print(f"done: {arch} v={v}d rep={rep} clean={clean_s:.3f}s")

                # --- C++ contender -------------------------------------------
                for arch, th in (("cpp-1T", 1), ("cpp-MT", 8)):
                    _, wall, cpu_s, rss, events, clean_s = run(
                        [bench_cpu, str(th)] + sub)
                    seen[arch] = events
                    emit(w, arch, "clean", th, v, rep, clean_s, clean_s, events, rss, cpu_s)
                    print(f"done: {arch} v={v}d rep={rep} clean={clean_s:.3f}s")

                # --- CUDA ----------------------------------------------------
                if cuda:
                    # --verify on the first repeat runs the bitwise differential
                    # against CapEventExtractorFlat inside the binary; a mismatch
                    # exits non-zero and run() aborts the sweep with the dump.
                    verify = ["--verify"] if rep == 1 else []
                    blob, wall, cpu_s, rss, events, clean_s = run(
                        [cuda] + verify + sub)
                    seen["cuda"] = events
                    emit(w, "cuda", "clean", 1, v, rep, clean_s, clean_s, events, rss, cpu_s)
                    m = _STAGE.search(blob)
                    if m:
                        kv = dict(p.split("=") for p in m.group(1).split())
                        sw.writerow({"files": v, "repeat": rep, "store_s": "0.000",
                                     **{k: kv.get(k, "0") for k in STAGE_FIELDS[2:-1]}})
                    print(f"done: cuda v={v}d rep={rep} clean={clean_s:.3f}s")

                distinct = set(seen.values())
                if len(distinct) > 1:
                    die("arches disagree on event count at "
                        f"{v} day-file(s), repeat {rep}: {seen}",
                        "a fast implementation that is wrong must not produce a "
                        "number; re-run the disagreeing arch with --verify")

                # --- e2e, only if the full build is present -------------------
                if mono:
                    for arch, th, flag in (("mono-1T", 1, ["--no-store"]),
                                           ("mono-1T", 1, []),
                                           ("mono-MT", 8, [])):
                        mode = "clean" if flag else "e2e"
                        out_db = os.path.join(ROOT, "bench_tmp.duckdb")
                        _, wall, cpu_s, rss, events, clean_s = run(
                            [mono] + flag + [out_db, "MCC", str(th)] + sub)
                        emit(w, arch, mode, th, v, rep, clean_s, wall, events, rss, cpu_s)
                        for stale in (out_db, out_db + ".t0.duckdb"):
                            if os.path.exists(stale):
                                os.remove(stale)
                        print(f"done: {arch} [{mode}] v={v}d rep={rep} clean={clean_s:.3f}s")

        # --- py-naive extrapolation (spec §6.3) ------------------------------
        # The interpreted loop is ~40 min per repeat at 28 day-files, so it is
        # measured at 1 day only. The transform is O(rows) with no cross-file
        # state, so scaling the median linearly is sound for `clean` mode. Rows
        # are labelled so nobody mistakes them for measurements.
        if naive_1day:
            naive_1day.sort()
            median_1d = naive_1day[len(naive_1day) // 2]
            for v in volumes:
                if v <= PY_NAIVE_MAX_FILES:
                    continue
                est = median_1d * v
                emit(w, "py-naive", "clean", 1, v, 0, est, est, 0, 0.0, 0.0,
                     note="extrapolated")
                print(f"note: py-naive v={v}d extrapolated to {est:.1f}s "
                      f"from the 1-day median")

        stage_fh.close()

    print(f"\nsweep complete\n  {RESULTS}\n  {STAGES}")
    print("\nPaste both files back. Summary:")
    with open(RESULTS) as fh:
        for line in fh:
            if not line.startswith("#"):
                print("  " + line.rstrip())


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Write `bench/README.md`**

```markdown
# CUDA cleaning benchmark

Measures the same cap-event transform in Python, C++, and CUDA on one machine.
See `docs/superpowers/specs/2026-08-10-cuda-cleaning-bench-design.md`.

## Prerequisites

| Platform | Needs |
|---|---|
| Windows | Visual Studio 2022 Build Tools, CUDA Toolkit, CMake, Python 3.9+ |
| Linux | gcc/clang with C++20, CUDA Toolkit, CMake, Python 3.9+ |
| macOS | Xcode CLT, CMake, Python 3.9+ (no CUDA — CPU and Python arches only) |

## Run

```
cmake -S . -B build -DMAS_BENCH_ONLY=ON -DMAS_ENABLE_CUDA=ON
cmake --build build --config Release
pip install -r bench/requirements-bench.txt
python bench/run_bench_cuda.py --data telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02.zip
```

`--config Release` is required on Windows (MSVC is multi-config) and harmless
elsewhere. Add `--quick` to run the 1-day volume only.

Outputs `bench/results_cuda.csv` and `bench/results_cuda_stages.csv`. Paste both
back.

## Adding the `e2e` rows

`MAS_BENCH_ONLY=ON` builds no DuckDB, so there is no store to write and no `e2e`
mode. For those rows, configure the full build as well:

```
cmake -S . -B build -DMAS_ENABLE_CUDA=ON -DMAS_ENABLE_ZMQ=OFF
cmake --build build --config Release
```

The driver picks up `mas_monolith` automatically and adds the `e2e` rows.

## If it fails

Every failure names what broke and the command that fixes it. Paste the whole
output — a correctness mismatch prints the first ten differing events with all
nine fields, which is enough to fix the bug without access to the machine.
```

- [ ] **Step 3: Run the driver locally, CPU and Python only**

Run: `.venv/bin/python bench/run_bench_cuda.py --data telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02 --quick`
Expected: completes; prints `done:` lines for `py-naive`, `py-numpy`, `cpp-1T`, `cpp-MT`, plus `mono-*` if `build/mas_monolith` exists; warns that `mas_cuda_clean` is missing; writes both CSVs. All four contenders must report `765711 events`.

- [ ] **Step 4: Check the CSV**

Run: `head -20 bench/results_cuda.csv`
Expected: provenance comment lines, then the header, then rows. `py-naive` should be dramatically slower than `py-numpy`, and both slower than `cpp-1T`.

- [ ] **Step 5: Commit**

```bash
git add bench/run_bench_cuda.py bench/README.md
git commit -m "feat(bench): portable driver replacing the bash harness

run_bench.sh needs bash, /usr/bin/time -l, unzip, find, sed and awk, and works
around macOS bash 3.2's missing associative arrays. None of that exists on the
Windows box this has to run on. Python was already required -- it is two of the
contenders -- so the driver removes six dependencies and adds none.

Binaries report their own timings, so nothing wraps them. Arches with missing
binaries are skipped with a warning rather than aborting, which is what lets one
script cover the portable build and the full build."
```

---

## Task 10: Plots and documentation

**Files:**
- Modify: `python/bench_plots.py` (add `--cuda`)
- Modify: `README.md` (new subsection under `## Benchmarking`)
- Modify: `docs/validation-log.md` (append an entry)

**Interfaces:**
- Consumes: `bench/results_cuda.csv`, `bench/results_cuda_stages.csv`
- Produces: `docs/bench/cuda_throughput.png`, `docs/bench/cuda_scaling.png`, `docs/bench/cuda_stages.png`

- [ ] **Step 1: Add `--cuda` to `bench_plots.py`**

Append to `python/bench_plots.py`, and wire `--cuda` into `main()` so it calls `render_cuda(...)` instead of `render(...)`:

```python
def render_cuda(results_csv, stages_csv, out_dir):
    """Three plots for the CUDA sweep (spec §6.6)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    os.makedirs(out_dir, exist_ok=True)
    df = pd.read_csv(results_csv, comment="#")
    med = (df.groupby(["arch", "mode", "files"])["clean_s"]
             .median().reset_index())

    one = med[med["files"] == med["files"].min()]
    fig, ax = plt.subplots(figsize=(9, 5))
    labels = [f"{a}\n[{m}]" for a, m in zip(one["arch"], one["mode"])]
    ax.bar(labels, one["clean_s"])
    ax.set_yscale("log")
    ax.set_ylabel("clean time, s (log)")
    ax.set_title(f"Cleaning one day-file ({int(one['files'].iloc[0])} file)")
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "cuda_throughput.png"), dpi=150)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(9, 5))
    for (arch, mode), grp in med.groupby(["arch", "mode"]):
        grp = grp.sort_values("files")
        ax.plot(grp["files"], grp["clean_s"], marker="o", label=f"{arch} [{mode}]")
    ax.set_xlabel("day-files")
    ax.set_ylabel("clean time, s (log)")
    ax.set_yscale("log")
    ax.legend(fontsize=8)
    ax.set_title("Scaling with volume")
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "cuda_scaling.png"), dpi=150)
    plt.close(fig)

    if os.path.exists(stages_csv):
        st = pd.read_csv(stages_csv).groupby("files").median().reset_index()
        cols = ["read_s", "h2d_s", "index_s", "parse_s", "delta_s",
                "compact_s", "d2h_s"]
        fig, ax = plt.subplots(figsize=(9, 5))
        bottom = [0.0] * len(st)
        for c in cols:
            ax.bar(st["files"].astype(str), st[c], bottom=bottom, label=c)
            bottom = [b + v for b, v in zip(bottom, st[c])]
        ax.set_xlabel("day-files")
        ax.set_ylabel("seconds")
        ax.legend(fontsize=8)
        ax.set_title("CUDA stage breakdown")
        fig.tight_layout()
        fig.savefig(os.path.join(out_dir, "cuda_stages.png"), dpi=150)
        plt.close(fig)
```

- [ ] **Step 2: Generate the plots from the local CPU-only sweep**

Run: `.venv/bin/python python/bench_plots.py --cuda && ls docs/bench/`
Expected: `cuda_throughput.png` and `cuda_scaling.png` exist. `cuda_stages.png` is absent until CUDA rows exist — that is correct, not a failure.

- [ ] **Step 3: Add the README subsection**

Insert under `## Benchmarking` in `README.md`, after the existing content:

```markdown
### CUDA cleaning benchmark

`CapEventExtractor` never reads state older than the previous row — every branch
of `process()` leaves `last_count_[h]` equal to the current row's count. So the
transform is element-wise over 3,110,364 independent (row, head) pairs per
day-file, not 36 sequential chains, and it ports to the GPU cleanly.

`bench/run_bench_cuda.py` measures the same transform five ways on one machine:
pure-Python (`oracle.py`), vectorized Python (`clean_vectorized.py`), C++ 1-thread
and 8-thread (`bench_cpu`), and CUDA (`mas_cuda_clean`).

```bash
cmake -S . -B build -DMAS_BENCH_ONLY=ON -DMAS_ENABLE_CUDA=ON
cmake --build build --config Release
python bench/run_bench_cuda.py --data telemetry_..._2026-02.zip
```

`MAS_BENCH_ONLY=ON` builds only the cleaning core and the benchmark binaries —
no DuckDB, no ZeroMQ, nothing downloaded — so it configures on any machine with
CMake, a C++20 compiler, and Python. See `bench/README.md` for the Windows path
and `docs/superpowers/specs/2026-08-10-cuda-cleaning-bench-design.md` for the
design.
```

- [ ] **Step 4: Append the validation-log entry**

Append to `docs/validation-log.md`:

```markdown
### CUDA cleaning pipeline and the three-way benchmark

The dedup was ruled a poor GPU fit in the 2026-07-04 spec (§3: "GPU acceleration
is optional stretch for analytics only, not the cleaning core") on the premise
that it is a sequential per-head scan. It is not. Every branch of
`CapEventExtractor::process` ends with `last = c`, and the held branch is entered
only when `c == *last` — so `last_count_[h]` after row `i` is always
`llround(count[i][h])`, and the transform never reads state older than one row.

`tests/test_cap_event_extractor_flat.cpp` is that claim as a test: the
element-wise `extract_flat` and the shipped stateful extractor produce identical
`CapEvent` vectors — all nine fields — on the edge cases and on a real day-file
(765,711 events). It runs with no GPU.

Two timing modes, because at GPU speeds the DuckDB write is two orders of
magnitude larger than the transform and would flatten every arch into the same
number. `clean` is the comparison; `e2e` is the deployment truth.

**Numbers: pending.** The development machine is an Apple M3 with no NVIDIA GPU
and no `nvcc`, so the CUDA path has never been compiled, let alone measured. It
is written, and its correctness gate (`mas_cuda_clean --verify`, bitwise against
`CapEventExtractorFlat`) runs as part of the sweep. To close this:

    cmake -S . -B build -DMAS_BENCH_ONLY=ON -DMAS_ENABLE_CUDA=ON
    cmake --build build --config Release
    python bench/run_bench_cuda.py --data telemetry_..._2026-02.zip

`py-naive` is measured at the 1-day volume only — 28 day-files is roughly two
hours of interpreted loop — and its larger volumes are linear extrapolations,
labelled as such. The transform is O(rows) with no cross-file state, so the
extrapolation is sound for `clean`; it is not claimed for `e2e`.
```

- [ ] **Step 5: Verify nothing regressed**

Run: `build/unit_tests 2>&1 | tail -3 && .venv/bin/python -m pytest python/tests -q 2>&1 | tail -3`
Expected: `[  PASSED  ] 89 tests.` and `224 passed, 5 skipped`.

- [ ] **Step 6: Commit**

```bash
git add python/bench_plots.py README.md docs/validation-log.md docs/bench
git commit -m "docs: the CUDA benchmark, and what is still unmeasured

The validation-log entry records the element-wise finding, the test that proves
it, and — plainly — that no CUDA number exists yet, with the command that
produces one. The same treatment the hosted-Anthropic gap got in Plan 7."
```

---

## After the sweep runs

The user pastes `bench/results_cuda.csv` and `bench/results_cuda_stages.csv`
back. Then:

1. Commit both CSVs.
2. Run `python python/bench_plots.py --cuda` to regenerate all three plots, now
   including the stage breakdown.
3. Replace the "Numbers: pending" paragraph in `docs/validation-log.md` with the
   measured figures, and state plainly which tier is fastest for `clean` and
   which for `e2e` — spec §11 criterion 7.
4. If `--verify` failed, the output carries the first ten differing events with
   all nine fields. Fix `parse_num` or `delta_kernel` from that, per spec §10 R2.
