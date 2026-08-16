# Cleaning Core & Dedup Pipeline — Implementation Plan

> **[SUPERSEDED 2026-08-11 — event identity.]** This document teaches
> `UNIQUE(machine_id, head_id, cap_seq)` as the store's identity. That key was
> discarded on 2026-08-11: the PLC's Count register resets mid-month, a closure
> recorded later can carry an already-used `cap_seq`, and keying on it dropped
> distinct physical caps onto older rows — February persisted 21,872,663 events
> as 14,372,237 rows, with 18,721 of head 1's colliding day-17 closures carrying
> a different torque. The store now keys on `UNIQUE(machine_id, head_id, ts)`
> and refuses a cap_seq-keyed store on open (`DuckDbEventStore.cpp`). See
> README § Database Design and docs/validation-log.md ("Event identity").
> The text below is preserved as written; read every `cap_seq` key claim
> through this notice.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the C++ core that turns raw 1 Hz Equatorque telemetry CSV into a deduplicated stream of real cap events (one event per per-head `Count` increment), TDD'd, plus a `clean` CLI and a Python reference oracle for cross-checking against real data.

**Architecture:** A pure, stateful `CapEventExtractor` (no I/O, no transport, no DB — the SRP domain core) consumes `RawRow` structs and emits `CapEvent`s. A `CsvRawReader` parses the real CSV layout into `RawRow`s. A thin `clean_file` pipeline wires reader→extractor→CSV writer, exposed by a `clean` CLI. A standalone Python `oracle.py` reimplements the same dedup for independent validation. This is Plan 1 of 8; it produces working, testable software with zero infrastructure dependencies (no ZeroMQ/DuckDB yet).

**Tech Stack:** C++17, CMake (FetchContent), GoogleTest 1.14, Python 3 (stdlib only).

## Global Constraints

- **Language split:** C++17 for the core; Python (stdlib only here) for the oracle. Copied from spec §4.
- **Heads:** exactly `NUM_HEADS = 36`. Spec §2.
- **Fault code:** `status == 65.0` is a fault (rare, ~10/day). Verify against real data — spec §14 Open Question 1. Spec §2.
- **Idempotency key (downstream):** cap events are uniquely identified by `(machine_id, head_id, cap_seq)`. Preserve these fields; persistence uses them later. Spec §6.
- **Timestamps preserved:** every emitted event carries the poll `ts` of the increment. Spec §1.
- **Never commit AROL data:** `*.csv` / `*.zip` are gitignored; tests generate synthetic fixtures, never real data. Spec §12.
- **Discipline:** DRY, YAGNI, TDD (red→green→commit), frequent commits.

---

## File Structure

- `CMakeLists.txt` — build: `mas_core` lib, `clean` exe, `unit_tests`. Grows across tasks.
- `core/include/mas/CapEvent.hpp` — `RawRow`, `CapEvent`, `NUM_HEADS`, `is_fault_status`. (Types only.)
- `core/include/mas/CapEventExtractor.hpp` / `core/src/CapEventExtractor.cpp` — the dedup core.
- `core/include/mas/CsvRawReader.hpp` / `core/src/CsvRawReader.cpp` — CSV → `RawRow`.
- `core/include/mas/Pipeline.hpp` / `core/src/Pipeline.cpp` — `clean_file(in, out, machine_id)`.
- `core/src/clean_main.cpp` — the `clean` CLI (thin wrapper).
- `tests/test_cap_event_extractor.cpp` — extractor unit + property tests.
- `tests/test_csv_raw_reader.cpp` — reader tests.
- `tests/test_pipeline.cpp` — end-to-end `clean_file` test.
- `python/oracle.py` — reference dedup implementation.
- `python/test_oracle.py` — pytest for the oracle.
- `python/validate_real.py` — manual cross-check against a real local file.

**Build/run reference** (used by every task):

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release   # first run downloads GoogleTest (network)
cmake --build build -j
ctest --test-dir build --output-on-failure
# or run a single test:
./build/unit_tests --gtest_filter='CapEventExtractor.EmitsOneEventOnSingleIncrement'
```

---

### Task 1: Scaffold + extractor core (seed / increment / held / per-head)

**Files:**
- Create: `CMakeLists.txt`, `core/include/mas/CapEvent.hpp`, `core/include/mas/CapEventExtractor.hpp`, `core/src/CapEventExtractor.cpp`, `tests/test_cap_event_extractor.cpp`

**Interfaces:**
- Produces: `mas::RawRow`, `mas::CapEvent`, `mas::NUM_HEADS`, `mas::is_fault_status(double)`, and `class mas::CapEventExtractor` with `void process(const RawRow&, std::vector<CapEvent>&)`.

- [ ] **Step 1: Create the build file**

`CMakeLists.txt`:

```cmake
cmake_minimum_required(VERSION 3.16)
project(mas_telemetry LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

include(FetchContent)
FetchContent_Declare(googletest
  URL https://github.com/google/googletest/archive/refs/tags/v1.14.0.zip)
set(gtest_force_shared_crt ON CACHE BOOL "" FORCE)
FetchContent_MakeAvailable(googletest)

add_library(mas_core
  core/src/CapEventExtractor.cpp)
target_include_directories(mas_core PUBLIC core/include)

enable_testing()
add_executable(unit_tests
  tests/test_cap_event_extractor.cpp)
target_link_libraries(unit_tests PRIVATE mas_core GTest::gtest_main)
include(GoogleTest)
gtest_discover_tests(unit_tests)
```

- [ ] **Step 2: Create the type header**

`core/include/mas/CapEvent.hpp`:

```cpp
#pragma once
#include <array>
#include <string>

namespace mas {

inline constexpr int NUM_HEADS = 36;

// AROL Equatorque status codes: 0 = idle/held, 2 = OK cap, 65 = fault (rare).
// Verify the fault set against real data (spec §14, Open Question 1).
inline bool is_fault_status(double status) {
    return status == 65.0;
}

// One raw 1 Hz poll: timestamp + per-head Count / AppTorque / Status.
struct RawRow {
    std::string ts;
    std::array<double, NUM_HEADS> count{};
    std::array<double, NUM_HEADS> torque{};
    std::array<double, NUM_HEADS> status{};
};

// One real cap event (or a reset marker) emitted by the extractor.
struct CapEvent {
    int head_id = 0;          // 1..36
    std::string ts;
    long long cap_seq = 0;    // head Count at this cap
    double app_torque = 0.0;
    double status = 0.0;
    int delta = 0;            // caps since last observation; >1 => aggregated
    bool is_fault = false;
    bool aggregated = false;
    bool reset = false;       // true => counter-reset marker
};

} // namespace mas
```

- [ ] **Step 3: Declare the extractor**

`core/include/mas/CapEventExtractor.hpp`:

```cpp
#pragma once
#include "mas/CapEvent.hpp"
#include <array>
#include <optional>
#include <vector>

namespace mas {

// Stateful, per-head. Feed rows in timestamp order; emitted events are
// appended to `out`. Not thread-safe: one instance per stream/head-partition.
class CapEventExtractor {
public:
    void process(const RawRow& row, std::vector<CapEvent>& out);

private:
    std::array<std::optional<long long>, NUM_HEADS> last_count_{};
};

} // namespace mas
```

- [ ] **Step 4: Create a stub implementation (so the test compiles but fails)**

`core/src/CapEventExtractor.cpp`:

```cpp
#include "mas/CapEventExtractor.hpp"

namespace mas {

void CapEventExtractor::process(const RawRow&, std::vector<CapEvent>&) {
    // stub — no behavior yet
}

} // namespace mas
```

- [ ] **Step 5: Write the failing tests**

`tests/test_cap_event_extractor.cpp`:

```cpp
#include "mas/CapEventExtractor.hpp"
#include <gtest/gtest.h>
#include <initializer_list>
#include <string>
#include <utility>
#include <vector>

namespace {

// Build a RawRow. `caps` sets Count for the given 1-based heads; torque/status
// apply to those heads too. Unlisted heads stay at 0.
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

TEST(CapEventExtractor, EmitsOneEventOnSingleIncrement) {
    mas::CapEventExtractor ex;
    std::vector<mas::CapEvent> out;
    ex.process(makeRow("t0", {{1, 100}}), out);   // seed
    ex.process(makeRow("t1", {{1, 101}}), out);   // increment
    ASSERT_EQ(out.size(), 1u);
    EXPECT_EQ(out[0].head_id, 1);
    EXPECT_EQ(out[0].cap_seq, 101);
    EXPECT_FALSE(out[0].reset);
}

TEST(CapEventExtractor, HeldRowsEmitNothing) {
    mas::CapEventExtractor ex;
    std::vector<mas::CapEvent> out;
    ex.process(makeRow("t0", {{1, 100}}), out);
    ex.process(makeRow("t1", {{1, 100}}), out);   // held
    ex.process(makeRow("t2", {{1, 100}}), out);   // held
    EXPECT_TRUE(out.empty());
}

TEST(CapEventExtractor, SeedRowEmitsNothing) {
    mas::CapEventExtractor ex;
    std::vector<mas::CapEvent> out;
    ex.process(makeRow("t0", {{1, 500}}), out);   // first observation
    EXPECT_TRUE(out.empty());
}

TEST(CapEventExtractor, HeadsAreIndependent) {
    mas::CapEventExtractor ex;
    std::vector<mas::CapEvent> out;
    ex.process(makeRow("t0", {{1, 10}, {5, 70}}), out);   // seed both
    ex.process(makeRow("t1", {{1, 11}, {5, 70}}), out);   // head 1 +1, head 5 held
    ASSERT_EQ(out.size(), 1u);
    EXPECT_EQ(out[0].head_id, 1);
}

} // namespace
```

- [ ] **Step 6: Configure, build, run — verify FAIL**

Run:
```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build --output-on-failure
```
Expected: `EmitsOneEventOnSingleIncrement` and `HeadsAreIndependent` FAIL (`out.size()` is 0, expected 1). `HeldRowsEmitNothing` and `SeedRowEmitsNothing` pass (stub emits nothing).

- [ ] **Step 7: Implement the minimal extractor**

Replace `core/src/CapEventExtractor.cpp`:

```cpp
#include "mas/CapEventExtractor.hpp"
#include <cmath>

namespace mas {

void CapEventExtractor::process(const RawRow& row, std::vector<CapEvent>& out) {
    for (int h = 0; h < NUM_HEADS; ++h) {
        const long long c = std::llround(row.count[h]);
        auto& last = last_count_[h];
        if (!last.has_value()) {          // first observation: seed, no event
            last = c;
            continue;
        }
        if (c > *last) {                  // real cap applied
            CapEvent e;
            e.head_id = h + 1;
            e.ts = row.ts;
            e.cap_seq = c;
            e.delta = 1;                  // generalized in Task 2
            e.aggregated = false;         // generalized in Task 2
            e.is_fault = false;           // set in Task 3
            e.reset = false;
            out.push_back(e);
            last = c;
        }
        // held (c == *last): emit nothing
    }
}

} // namespace mas
```

- [ ] **Step 8: Build, run — verify PASS**

Run:
```bash
cmake --build build -j && ctest --test-dir build --output-on-failure
```
Expected: all 4 tests PASS.

- [ ] **Step 9: Commit**

```bash
git add CMakeLists.txt core/ tests/
git commit -m "feat(core): cap-event extractor — seed/increment/held, per-head"
```

---

### Task 2: Generalize delta and the `aggregated` flag

**Files:**
- Modify: `core/src/CapEventExtractor.cpp`
- Modify: `tests/test_cap_event_extractor.cpp`

**Interfaces:**
- Consumes: `CapEventExtractor::process` (Task 1).
- Produces: on an increment, `delta == cap_seq - previous_count` and `aggregated == (delta > 1)`.

- [ ] **Step 1: Add the failing test**

Append to `tests/test_cap_event_extractor.cpp` (before the closing `} // namespace`):

```cpp
TEST(CapEventExtractor, SingleIncrementHasDeltaOne) {
    mas::CapEventExtractor ex;
    std::vector<mas::CapEvent> out;
    ex.process(makeRow("t0", {{1, 100}}), out);
    ex.process(makeRow("t1", {{1, 101}}), out);
    ASSERT_EQ(out.size(), 1u);
    EXPECT_EQ(out[0].delta, 1);
    EXPECT_FALSE(out[0].aggregated);
}

TEST(CapEventExtractor, DeltaGreaterThanOneIsAggregated) {
    mas::CapEventExtractor ex;
    std::vector<mas::CapEvent> out;
    ex.process(makeRow("t0", {{1, 100}}), out);
    ex.process(makeRow("t1", {{1, 103}}), out);   // production faster than polling
    ASSERT_EQ(out.size(), 1u);
    EXPECT_EQ(out[0].delta, 3);
    EXPECT_TRUE(out[0].aggregated);
}
```

- [ ] **Step 2: Run — verify FAIL**

Run: `cmake --build build -j && ./build/unit_tests --gtest_filter='*Aggregated*'`
Expected: `DeltaGreaterThanOneIsAggregated` FAILS (`delta` is hardcoded 1, expected 3).

- [ ] **Step 3: Generalize the increment branch**

In `core/src/CapEventExtractor.cpp`, inside `if (c > *last)`, replace the `e.delta`/`e.aggregated` lines:

```cpp
        if (c > *last) {                  // real cap applied
            const int delta = static_cast<int>(c - *last);
            CapEvent e;
            e.head_id = h + 1;
            e.ts = row.ts;
            e.cap_seq = c;
            e.delta = delta;
            e.aggregated = delta > 1;
            e.is_fault = false;           // set in Task 3
            e.reset = false;
            out.push_back(e);
            last = c;
        }
```

- [ ] **Step 4: Run — verify PASS**

Run: `cmake --build build -j && ctest --test-dir build --output-on-failure`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add core/src/CapEventExtractor.cpp tests/test_cap_event_extractor.cpp
git commit -m "feat(core): compute delta and aggregated flag on increments"
```

---

### Task 3: Capture torque, status, and the fault flag

**Files:**
- Modify: `core/src/CapEventExtractor.cpp`
- Modify: `tests/test_cap_event_extractor.cpp`

**Interfaces:**
- Produces: emitted event carries `app_torque`, `status` from the increment row, and `is_fault == is_fault_status(status)`.

- [ ] **Step 1: Add the failing test**

Append to `tests/test_cap_event_extractor.cpp`:

```cpp
TEST(CapEventExtractor, CapturesTorqueStatusAndFaultFlag) {
    mas::CapEventExtractor ex;
    std::vector<mas::CapEvent> out;
    ex.process(makeRow("t0", {{2, 200}}, 2.0, 2.0), out);        // seed, OK
    ex.process(makeRow("t1", {{2, 201}}, 1.907, 65.0), out);     // fault cap
    ASSERT_EQ(out.size(), 1u);
    EXPECT_DOUBLE_EQ(out[0].app_torque, 1.907);
    EXPECT_DOUBLE_EQ(out[0].status, 65.0);
    EXPECT_TRUE(out[0].is_fault);
}
```

- [ ] **Step 2: Run — verify FAIL**

Run: `cmake --build build -j && ./build/unit_tests --gtest_filter='*CapturesTorque*'`
Expected: FAILS (`app_torque`/`status` are 0, `is_fault` false).

- [ ] **Step 3: Copy torque/status/fault in the increment branch**

In `core/src/CapEventExtractor.cpp`, inside `if (c > *last)`, add three assignments (after `e.cap_seq = c;`):

```cpp
            e.app_torque = row.torque[h];
            e.status = row.status[h];
            e.is_fault = is_fault_status(row.status[h]);
```

(Remove the now-redundant `e.is_fault = false;` line in this branch.)

- [ ] **Step 4: Run — verify PASS**

Run: `cmake --build build -j && ctest --test-dir build --output-on-failure`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add core/src/CapEventExtractor.cpp tests/test_cap_event_extractor.cpp
git commit -m "feat(core): capture torque, status, and fault flag on cap events"
```

---

### Task 4: Counter reset marker + delta-sum invariant

**Files:**
- Modify: `core/src/CapEventExtractor.cpp`
- Modify: `tests/test_cap_event_extractor.cpp`

**Interfaces:**
- Produces: when `Count` decreases, emit a `CapEvent` with `reset == true`, `delta == 0`, `cap_seq == new count`; extractor re-seeds `last_count` to the new value.

- [ ] **Step 1: Add the failing tests**

Append to `tests/test_cap_event_extractor.cpp`:

```cpp
TEST(CapEventExtractor, CounterResetEmitsResetMarker) {
    mas::CapEventExtractor ex;
    std::vector<mas::CapEvent> out;
    ex.process(makeRow("t0", {{1, 100}}), out);
    ex.process(makeRow("t1", {{1, 40}}), out);    // reset / rollover
    ASSERT_EQ(out.size(), 1u);
    EXPECT_TRUE(out[0].reset);
    EXPECT_EQ(out[0].cap_seq, 40);
    EXPECT_EQ(out[0].delta, 0);
}

TEST(CapEventExtractor, DeltaSumEqualsFinalMinusInitialAcrossSpan) {
    mas::CapEventExtractor ex;
    std::vector<mas::CapEvent> out;
    const long long seq[] = {1000, 1000, 1001, 1001, 1004, 1004, 1005};
    for (int i = 0; i < 7; ++i)
        ex.process(makeRow("t" + std::to_string(i), {{3, seq[i]}}), out);
    long long sum = 0;
    for (const auto& e : out) sum += e.delta;
    EXPECT_EQ(sum, 1005 - 1000);   // 5 caps, independent of poll cadence
}
```

- [ ] **Step 2: Run — verify FAIL**

Run: `cmake --build build -j && ./build/unit_tests --gtest_filter='*Reset*'`
Expected: `CounterResetEmitsResetMarker` FAILS (nothing emitted on decrease).

- [ ] **Step 3: Add the reset branch**

In `core/src/CapEventExtractor.cpp`, after the `if (c > *last) { ... }` block and before the closing comment, add:

```cpp
        else if (c < *last) {             // counter reset / rollover
            CapEvent e;
            e.head_id = h + 1;
            e.ts = row.ts;
            e.cap_seq = c;
            e.app_torque = row.torque[h];
            e.status = row.status[h];
            e.delta = 0;
            e.is_fault = is_fault_status(row.status[h]);
            e.aggregated = false;
            e.reset = true;
            out.push_back(e);
            last = c;
        }
```

- [ ] **Step 4: Run — verify PASS**

Run: `cmake --build build -j && ctest --test-dir build --output-on-failure`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add core/src/CapEventExtractor.cpp tests/test_cap_event_extractor.cpp
git commit -m "feat(core): emit reset marker on counter rollback; add delta-sum invariant test"
```

---

### Task 5: CSV raw reader

**Files:**
- Create: `core/include/mas/CsvRawReader.hpp`, `core/src/CsvRawReader.cpp`, `tests/test_csv_raw_reader.cpp`
- Modify: `CMakeLists.txt`

**Interfaces:**
- Produces: `class mas::CsvRawReader` with `explicit CsvRawReader(const std::string& path)` and `bool next(RawRow& out)` (skips header, returns false at EOF). Column layout: field 0 = ts; fields `1..36` = Count; `37..72` = AppTorque; `73..108` = Status.

- [ ] **Step 1: Declare the reader**

`core/include/mas/CsvRawReader.hpp`:

```cpp
#pragma once
#include "mas/CapEvent.hpp"
#include <fstream>
#include <string>

namespace mas {

// Streams a raw telemetry CSV: timestamp + 36 Count + 36 AppTorque + 36 Status.
// Discards the header line on construction.
class CsvRawReader {
public:
    explicit CsvRawReader(const std::string& path);
    bool next(RawRow& out);   // false at EOF

private:
    std::ifstream in_;
};

} // namespace mas
```

- [ ] **Step 2: Implement the reader**

`core/src/CsvRawReader.cpp`:

```cpp
#include "mas/CsvRawReader.hpp"
#include <sstream>
#include <string>
#include <vector>

namespace mas {

CsvRawReader::CsvRawReader(const std::string& path) : in_(path) {
    std::string header;
    std::getline(in_, header);   // discard header
}

bool CsvRawReader::next(RawRow& out) {
    std::string line;
    while (std::getline(in_, line)) {
        if (line.empty()) continue;

        std::vector<std::string> f;
        f.reserve(1 + NUM_HEADS * 3);
        std::string cur;
        std::istringstream ss(line);
        while (std::getline(ss, cur, ',')) f.push_back(cur);
        if (f.size() < static_cast<size_t>(1 + NUM_HEADS * 3)) continue;

        out.ts = f[0];
        for (int h = 0; h < NUM_HEADS; ++h) {
            out.count[h]  = std::stod(f[1 + h]);
            out.torque[h] = std::stod(f[1 + NUM_HEADS + h]);
            out.status[h] = std::stod(f[1 + 2 * NUM_HEADS + h]);
        }
        return true;
    }
    return false;
}

} // namespace mas
```

- [ ] **Step 3: Add reader to the build**

In `CMakeLists.txt`, extend `mas_core` and `unit_tests` source lists:

```cmake
add_library(mas_core
  core/src/CapEventExtractor.cpp
  core/src/CsvRawReader.cpp)
target_include_directories(mas_core PUBLIC core/include)

enable_testing()
add_executable(unit_tests
  tests/test_cap_event_extractor.cpp
  tests/test_csv_raw_reader.cpp)
```

- [ ] **Step 4: Write the failing test**

`tests/test_csv_raw_reader.cpp`:

```cpp
#include "mas/CsvRawReader.hpp"
#include <gtest/gtest.h>
#include <cstdio>
#include <fstream>
#include <string>

namespace {

void writeFile(const std::string& path, const std::string& body) {
    std::ofstream o(path);
    o << body;
}

TEST(CsvRawReader, ParsesTimestampAndPerHeadColumns) {
    std::string header = "timestamp";
    for (int i = 0; i < 36; ++i) header += ",H Count";
    for (int i = 0; i < 36; ++i) header += ",H AppTorque";
    for (int i = 0; i < 36; ++i) header += ",H Status";

    std::string row = "2026-02-01T10:00:00.000";
    for (int i = 0; i < 36; ++i) row += (i == 0) ? ",100.0" : ",0.0";   // counts
    for (int i = 0; i < 36; ++i) row += (i == 0) ? ",1.5"   : ",0.0";   // torque
    for (int i = 0; i < 36; ++i) row += (i == 0) ? ",65.0"  : ",0.0";   // status

    const std::string path = "test_reader_input.csv";
    writeFile(path, header + "\n" + row + "\n");

    mas::CsvRawReader reader(path);
    mas::RawRow r;
    ASSERT_TRUE(reader.next(r));
    EXPECT_EQ(r.ts, "2026-02-01T10:00:00.000");
    EXPECT_DOUBLE_EQ(r.count[0], 100.0);
    EXPECT_DOUBLE_EQ(r.torque[0], 1.5);
    EXPECT_DOUBLE_EQ(r.status[0], 65.0);
    EXPECT_FALSE(reader.next(r));   // only one data row

    std::remove(path.c_str());
}

} // namespace
```

- [ ] **Step 5: Configure, build, run — verify PASS**

Run:
```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
./build/unit_tests --gtest_filter='CsvRawReader.*'
```
Expected: `ParsesTimestampAndPerHeadColumns` PASSES. (Re-run configure because `CMakeLists.txt` changed.)

- [ ] **Step 6: Commit**

```bash
git add CMakeLists.txt core/include/mas/CsvRawReader.hpp core/src/CsvRawReader.cpp tests/test_csv_raw_reader.cpp
git commit -m "feat(core): CSV raw reader for 109-column telemetry rows"
```

---

### Task 6: `clean_file` pipeline + `clean` CLI

**Files:**
- Create: `core/include/mas/Pipeline.hpp`, `core/src/Pipeline.cpp`, `core/src/clean_main.cpp`, `tests/test_pipeline.cpp`
- Modify: `CMakeLists.txt`

**Interfaces:**
- Consumes: `CsvRawReader`, `CapEventExtractor` (Tasks 1–5).
- Produces: `long long mas::clean_file(const std::string& in_path, const std::string& out_path, const std::string& machine_id)` — writes a `cap_events` CSV (header: `machine_id,head_id,ts,cap_seq,app_torque,status,delta,is_fault,aggregated,reset`), returns the event count. `clean` CLI: `clean <raw_in.csv> <events_out.csv> [machine_id]`.

- [ ] **Step 1: Declare the pipeline**

`core/include/mas/Pipeline.hpp`:

```cpp
#pragma once
#include <string>

namespace mas {

// Read raw telemetry CSV at in_path, write cap_events CSV at out_path.
// Returns the number of cap events written.
long long clean_file(const std::string& in_path, const std::string& out_path,
                     const std::string& machine_id);

} // namespace mas
```

- [ ] **Step 2: Implement the pipeline**

`core/src/Pipeline.cpp`:

```cpp
#include "mas/Pipeline.hpp"
#include "mas/CapEventExtractor.hpp"
#include "mas/CsvRawReader.hpp"
#include <fstream>
#include <vector>

namespace mas {

long long clean_file(const std::string& in_path, const std::string& out_path,
                     const std::string& machine_id) {
    CsvRawReader reader(in_path);
    std::ofstream out(out_path);
    out << "machine_id,head_id,ts,cap_seq,app_torque,status,delta,is_fault,aggregated,reset\n";

    CapEventExtractor ex;
    RawRow row;
    std::vector<CapEvent> buf;
    long long n = 0;
    while (reader.next(row)) {
        buf.clear();
        ex.process(row, buf);
        for (const auto& e : buf) {
            out << machine_id << ',' << e.head_id << ',' << e.ts << ','
                << e.cap_seq << ',' << e.app_torque << ',' << e.status << ','
                << e.delta << ',' << (e.is_fault ? 1 : 0) << ','
                << (e.aggregated ? 1 : 0) << ',' << (e.reset ? 1 : 0) << '\n';
            ++n;
        }
    }
    return n;
}

} // namespace mas
```

- [ ] **Step 3: Write the CLI wrapper**

`core/src/clean_main.cpp`:

```cpp
#include "mas/Pipeline.hpp"
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    if (argc < 3) {
        std::cerr << "usage: clean <raw_in.csv> <events_out.csv> [machine_id]\n";
        return 2;
    }
    const std::string machine = (argc > 3) ? argv[3] : "MCC";
    const long long n = mas::clean_file(argv[1], argv[2], machine);
    std::cerr << "wrote " << n << " cap events\n";
    return 0;
}
```

- [ ] **Step 4: Add pipeline, CLI, and test to the build**

In `CMakeLists.txt`, extend `mas_core`, add the `clean` executable, and extend `unit_tests`:

```cmake
add_library(mas_core
  core/src/CapEventExtractor.cpp
  core/src/CsvRawReader.cpp
  core/src/Pipeline.cpp)
target_include_directories(mas_core PUBLIC core/include)

add_executable(clean core/src/clean_main.cpp)
target_link_libraries(clean PRIVATE mas_core)

enable_testing()
add_executable(unit_tests
  tests/test_cap_event_extractor.cpp
  tests/test_csv_raw_reader.cpp
  tests/test_pipeline.cpp)
```

- [ ] **Step 5: Write the failing end-to-end test**

`tests/test_pipeline.cpp`:

```cpp
#include "mas/Pipeline.hpp"
#include <gtest/gtest.h>
#include <cstdio>
#include <fstream>
#include <sstream>
#include <string>

namespace {

void writeFile(const std::string& path, const std::string& body) {
    std::ofstream o(path);
    o << body;
}

// One raw row: head 1 gets head1_count; all other columns 0. Head 1 torque/status = 2.0.
std::string rawLine(const std::string& ts, long long head1_count) {
    std::string s = ts;
    for (int i = 0; i < 36; ++i) s += (i == 0) ? ("," + std::to_string(head1_count) + ".0") : ",0.0";
    for (int i = 0; i < 36; ++i) s += (i == 0) ? ",2.0" : ",0.0";
    for (int i = 0; i < 36; ++i) s += (i == 0) ? ",2.0" : ",0.0";
    return s;
}

size_t countDataLines(const std::string& path) {
    std::ifstream in(path);
    std::string line;
    size_t n = 0;
    bool first = true;
    while (std::getline(in, line)) {
        if (first) { first = false; continue; }   // skip header
        if (!line.empty()) ++n;
    }
    return n;
}

TEST(Pipeline, CleanFileEmitsOneEventPerIncrement) {
    const std::string in = "pipe_in.csv", out = "pipe_out.csv";
    std::string header = "timestamp";
    for (int i = 0; i < 108; ++i) header += ",c";

    std::ostringstream body;
    body << header << "\n";
    body << rawLine("t0", 100) << "\n";   // seed
    body << rawLine("t1", 100) << "\n";   // held
    body << rawLine("t2", 101) << "\n";   // +1
    body << rawLine("t3", 104) << "\n";   // +3 (aggregated)
    writeFile(in, body.str());

    const long long n = mas::clean_file(in, out, "MCCtest");
    EXPECT_EQ(n, 2);                       // two increment events
    EXPECT_EQ(countDataLines(out), 2u);

    std::remove(in.c_str());
    std::remove(out.c_str());
}

} // namespace
```

- [ ] **Step 6: Configure, build, run — verify PASS**

Run:
```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build --output-on-failure
```
Expected: all tests PASS, including `Pipeline.CleanFileEmitsOneEventPerIncrement`. The `clean` binary is now at `./build/clean`.

- [ ] **Step 7: Commit**

```bash
git add CMakeLists.txt core/include/mas/Pipeline.hpp core/src/Pipeline.cpp core/src/clean_main.cpp tests/test_pipeline.cpp
git commit -m "feat(core): clean_file pipeline and clean CLI"
```

---

### Task 7: Python reference oracle + real-data cross-check

**Files:**
- Create: `python/oracle.py`, `python/test_oracle.py`, `python/validate_real.py`

**Interfaces:**
- Consumes: same CSV layout as `CsvRawReader`; same dedup rules as `CapEventExtractor`.
- Produces: `oracle.extract(path) -> list[tuple]` where each tuple is `(head_id, ts, cap_seq, torque, status, delta, is_fault, aggregated, reset)`. `validate_real.py` prints the oracle event count for a real file and, given the C++ output CSV, asserts the counts match.

- [ ] **Step 1: Write the oracle**

`python/oracle.py`:

```python
"""Reference dedup: independent re-implementation of CapEventExtractor.
Used to cross-check the C++ core on real data (spec §11)."""
import csv
import sys

NUM_HEADS = 36
FAULT_CODES = {65.0}


def extract(path):
    last = [None] * NUM_HEADS
    events = []
    with open(path, newline="") as f:
        reader = csv.reader(f)
        next(reader)  # header
        for row in reader:
            if len(row) < 1 + NUM_HEADS * 3:
                continue
            ts = row[0]
            counts = [float(x) for x in row[1:1 + NUM_HEADS]]
            torque = [float(x) for x in row[1 + NUM_HEADS:1 + 2 * NUM_HEADS]]
            status = [float(x) for x in row[1 + 2 * NUM_HEADS:1 + 3 * NUM_HEADS]]
            for h in range(NUM_HEADS):
                c = round(counts[h])
                if last[h] is None:
                    last[h] = c
                    continue
                if c > last[h]:
                    delta = c - last[h]
                    events.append((h + 1, ts, c, torque[h], status[h],
                                   delta, status[h] in FAULT_CODES, delta > 1, False))
                    last[h] = c
                elif c < last[h]:
                    events.append((h + 1, ts, c, torque[h], status[h],
                                   0, status[h] in FAULT_CODES, False, True))
                    last[h] = c
    return events


if __name__ == "__main__":
    print(len(extract(sys.argv[1])))
```

- [ ] **Step 2: Write the failing pytest**

`python/test_oracle.py`:

```python
import csv
import os
import oracle


def _write_raw(path, rows):
    """rows: list of (ts, head1_count). Only head 1 varies; 109 columns."""
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        header = ["timestamp"] + [f"c{i}" for i in range(108)]
        w.writerow(header)
        for ts, c in rows:
            counts = [c] + [0.0] * 35
            torque = [2.0] + [0.0] * 35
            status = [2.0] + [0.0] * 35
            w.writerow([ts] + counts + torque + status)


def test_oracle_counts_increments_and_dedups_held(tmp_path):
    path = os.path.join(tmp_path, "raw.csv")
    _write_raw(path, [("t0", 100), ("t1", 100), ("t2", 101), ("t3", 104)])
    events = oracle.extract(path)
    assert len(events) == 2                 # t2 (+1) and t3 (+3)
    assert events[0][5] == 1                # delta of first event
    assert events[1][5] == 3 and events[1][7] is True   # aggregated
```

- [ ] **Step 3: Run — verify PASS**

Run:
```bash
cd python && python3 -m pytest test_oracle.py -v
```
Expected: `test_oracle_counts_increments_and_dedups_held` PASSES. (If `pytest` is unavailable: `pip install pytest` in a venv, or run `python3 -c "import oracle; ..."` manually.)

- [ ] **Step 4: Write the real-data cross-check script**

`python/validate_real.py`:

```python
"""Cross-check the C++ core against the Python oracle on a REAL local file.

Usage:
    python3 validate_real.py <raw_day.csv> [cpp_events_out.csv]

Prints the oracle event count. If the C++ output CSV is given, asserts its
data-row count equals the oracle's. Real data is never committed (spec §12)."""
import sys
import oracle


def cpp_event_count(path):
    with open(path) as f:
        return sum(1 for i, _ in enumerate(f) if i > 0)  # minus header


def main():
    raw = sys.argv[1]
    events = oracle.extract(raw)
    print(f"oracle events: {len(events)}")
    if len(sys.argv) > 2:
        n = cpp_event_count(sys.argv[2])
        print(f"cpp events:    {n}")
        assert n == len(events), f"MISMATCH: cpp {n} vs oracle {len(events)}"
        print("MATCH")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Manual real-data verification (documented, not CI)**

Run against one real day-file (extracted locally from the gitignored ZIP):
```bash
# from repo root, after unzipping one day file to /tmp (do NOT commit it):
./build/clean /tmp/telemetry_..._2026-02-01.csv /tmp/events.csv MCC
cd python && python3 validate_real.py /tmp/telemetry_..._2026-02-01.csv /tmp/events.csv
```
Expected: `MATCH`, and the oracle event count is on the order of the spec's measured **~765k events/day** (Appendix A). Record the exact number in the DOCUMENTATION deliverable.

- [ ] **Step 6: Commit**

```bash
git add python/oracle.py python/test_oracle.py python/validate_real.py
git commit -m "feat(oracle): Python reference dedup + real-data cross-check"
```

---

## Self-Review

**Spec coverage (Plan 1 scope = spec §6 dedup core + §11 testing for the core):**
- Count-increment extraction → Tasks 1–2. ✓
- Held-row dedup → Task 1. ✓
- `delta>1` aggregated flag → Task 2. ✓
- Torque/status/fault capture → Task 3. ✓
- Counter reset marker → Task 4. ✓
- Δ-sum invariant (property test) → Task 4. ✓
- CSV layout parsing (109 cols) → Task 5. ✓
- Event schema fields (`machine_id,head_id,ts,cap_seq,...`) → Task 6, matches spec §6 schema. ✓
- Python correctness oracle + real-data cross-check vs ~765k/day → Task 7. ✓
- SOLID: extractor has zero I/O (SRP/DIP seam preserved — no `<fstream>`/DB in `CapEventExtractor`). ✓
- Out of Plan-1 scope (later plans): OpenMP parallelism (Plan 7), DuckDB/Parquet persistence & idempotent upsert (Plan 2), ZeroMQ agents (Plan 3). Noted, not gaps.

**Placeholder scan:** No TBD/TODO/"handle edge cases"/"add validation" — every step has concrete code or an exact command. ✓

**Type consistency:** `RawRow`/`CapEvent` field names (`count`, `torque`, `status`, `cap_seq`, `app_torque`, `delta`, `is_fault`, `aggregated`, `reset`) are identical across Tasks 1–7 and match the CSV writer header in Task 6 and the oracle tuple in Task 7. `process(const RawRow&, std::vector<CapEvent>&)`, `CsvRawReader::next(RawRow&)`, and `clean_file(in, out, machine_id)` signatures are stable wherever referenced. Column offsets (`1..36` count, `37..72` torque, `73..108` status) match between `CsvRawReader` (Task 5), the pipeline test (Task 6), and `oracle.py` (Task 7). ✓
