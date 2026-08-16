# Event Store & DuckDB Persistence — Implementation Plan (Plan 2 of 8)

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

**Goal:** Put the `IEventStore` persistence seam under the cleaning pipeline and implement a DuckDB-backed cap-event store with the idempotent upsert on `(machine_id, head_id, cap_seq)`, plus Parquet export — so reprocessing any day-file is safe (spec §6, §10, §12).

**Architecture:** Extract the CSV-writing code already inside `clean_file` into a `CsvEventStore : IEventStore` adapter, and add `DuckDbEventStore : IEventStore` (pimpl, so no public header ever includes `duckdb.hpp` — the DIP seam of spec §7). `clean_file` gains an overload that takes any `IEventStore&` and batches events (spec §5.3 "batched insert"). Idempotency: DuckDB `UNIQUE(machine_id, head_id, cap_seq)` + Appender into a staging table + `INSERT OR IGNORE` merge. The `clean` CLI dispatches on output extension: `.duckdb` → DuckDB store, anything else → CSV (unchanged behavior).

**Tech Stack:** C++20 (bump from 17 — needed for `std::span` in the spec-§7 interface and `std::string::ends_with`), CMake FetchContent, DuckDB **v1.2.2 prebuilt binary** (`libduckdb-osx-universal.zip` / `libduckdb-linux-amd64.zip` — no source build), GoogleTest 1.14.

## Global Constraints

- **C++ standard:** bump `CMAKE_CXX_STANDARD` to **20** (spec §15 allows "C++17/20"; spec §7 interface is `virtual void write(std::span<const CapEvent>)`).
- **Heads:** `NUM_HEADS = 36` (spec §2) — unchanged, already in `CapEvent.hpp`.
- **Idempotency key:** `UNIQUE (machine_id, head_id, cap_seq)`; reprocessing any day is safe (spec §6, §10). This is the core deliverable of this plan.
- **Schema (spec §6):** `cap_events(machine_id TEXT, head_id SMALLINT, ts TIMESTAMP, cap_seq BIGINT, app_torque REAL, status REAL, delta INT, is_fault BOOL, aggregated BOOL)` — **plus one extension:** `is_reset BOOLEAN`, because Plan 1 folded the spec's `ResetMarker` into `CapEvent.reset`. Named `is_reset` (not `reset`) because `RESET` is a DuckDB SQL keyword. The CSV store keeps its existing `reset` header column.
- **Timestamps:** real data uses `2026-02-27T16:00:00.000` (ISO-8601, `T` separator, milliseconds) — casts cleanly to DuckDB `TIMESTAMP`. Synthetic fixtures that go through DuckDB **must** use this format (extractor-only fixtures may keep `"t0"` strings).
- **DuckDB version:** pin **v1.2.2** prebuilt release asset via FetchContent. Do not build DuckDB from source (30+ min build).
- **DIP (spec §7):** domain headers (`CapEvent.hpp`, `CapEventExtractor.hpp`, `Pipeline.hpp`, `EventStore.hpp`) never include `duckdb.hpp`. Only `core/src/DuckDbEventStore.cpp` does (pimpl).
- **Existing sentinels stay:** `clean_file(in, out, machine)` returns `-1` (input unreadable), `-2` (output unwritable / write error). Tests `Pipeline.CleanFileReturnsMinusOneOnMissingInput` and `Pipeline.CleanFileReturnsMinusTwoOnUnwritableOutput` must keep passing.
- **Never commit AROL data or databases:** `*.csv`, `*.zip`, `*.duckdb`, `*.parquet` are gitignored; add `*.wal` (DuckDB write-ahead log) in Task 3.
- **Out of scope (later plans):** multi-process write concurrency (spec §14 Q4 — per-worker Parquet merged by sink, Plan 3+), raw store for the ingestion agent, OpenMP (Plan 7), ZeroMQ (Plan 3).
- **Discipline:** DRY, YAGNI, TDD (red→green→commit), frequent commits.

---

## File Structure

- `CMakeLists.txt` — modify: C++20; add `CsvEventStore.cpp`, `DuckDbEventStore.cpp` to `mas_core`; FetchContent DuckDB prebuilt + `duckdb_imported` target; link + rpath; new test files.
- `core/include/mas/EventStore.hpp` — **create**: `IEventStore` interface (types only, spec §7).
- `core/include/mas/CsvEventStore.hpp` / `core/src/CsvEventStore.cpp` — **create**: CSV adapter (code extracted from `Pipeline.cpp`).
- `core/include/mas/DuckDbEventStore.hpp` / `core/src/DuckDbEventStore.cpp` — **create**: DuckDB adapter, pimpl, idempotent upsert, `count()`, `export_parquet()`.
- `core/include/mas/Pipeline.hpp` / `core/src/Pipeline.cpp` — **modify**: add `clean_file(in, IEventStore&)` overload with batching; existing string overload becomes a thin wrapper.
- `core/src/clean_main.cpp` — **modify**: extension dispatch `.duckdb` vs CSV.
- `tests/test_pipeline.cpp` — **modify**: add fake-store seam test + DuckDB end-to-end idempotency test.
- `tests/test_duckdb_smoke.cpp` — **create**: dependency smoke test (`SELECT 42`).
- `tests/test_duckdb_event_store.cpp` — **create**: store unit tests (idempotency, persistence, parquet).
- `docs/validation-log.md` — **create** (Task 6): recorded real-data numbers.

**Build/run reference** (used by every task):

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release   # re-run whenever CMakeLists.txt changes; Task 2's first run downloads DuckDB (~50 MB, network)
cmake --build build -j
ctest --test-dir build --output-on-failure
# single test:
./build/unit_tests --gtest_filter='DuckDbEventStore.*'
```

---

### Task 1: `IEventStore` seam + `CsvEventStore` extraction (C++20 bump)

**Files:**
- Modify: `CMakeLists.txt`
- Create: `core/include/mas/EventStore.hpp`, `core/include/mas/CsvEventStore.hpp`, `core/src/CsvEventStore.cpp`
- Modify: `core/include/mas/Pipeline.hpp`, `core/src/Pipeline.cpp`
- Test: `tests/test_pipeline.cpp`

**Interfaces:**
- Consumes: `mas::CapEvent`, `mas::CsvRawReader`, `mas::CapEventExtractor` (Plan 1).
- Produces:
  - `struct mas::IEventStore { virtual void write(std::span<const CapEvent> events) = 0; virtual ~IEventStore() = default; };`
  - `class mas::CsvEventStore : public IEventStore` — ctor `(const std::string& out_path, const std::string& machine_id)`, throws `std::runtime_error` on open failure; `void close()` flushes and throws on write error.
  - `long long mas::clean_file(const std::string& in_path, IEventStore& store)` — returns event count, `-1` if input unreadable; store exceptions propagate. **Tasks 3–5 rely on these exact signatures.**

- [ ] **Step 1: Bump to C++20 and verify the toolchain**

In `CMakeLists.txt` change one line:

```cmake
set(CMAKE_CXX_STANDARD 20)
```

Run:
```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j && ctest --test-dir build --output-on-failure
```
Expected: all 15 existing tests PASS (AppleClang 17 handles C++20; GoogleTest 1.14 is C++20-clean).

- [ ] **Step 2: Create the interface header**

`core/include/mas/EventStore.hpp`:

```cpp
#pragma once
#include "mas/CapEvent.hpp"
#include <span>

namespace mas {

// Persistence seam (DIP, spec §7): the pipeline writes events through this
// interface and never sees CSV/DuckDB. Implementations own machine_id.
struct IEventStore {
    virtual void write(std::span<const CapEvent> events) = 0;
    virtual ~IEventStore() = default;
};

} // namespace mas
```

- [ ] **Step 3: Write the failing seam test**

Append to `tests/test_pipeline.cpp` (before the closing `} // namespace`), and add the include `#include "mas/EventStore.hpp"` at the top of the file:

```cpp
struct FakeEventStore : mas::IEventStore {
    std::vector<mas::CapEvent> got;
    void write(std::span<const mas::CapEvent> events) override {
        got.insert(got.end(), events.begin(), events.end());
    }
};

TEST(Pipeline, CleanFileWritesEventsToInjectedStore) {
    const std::string in = "pipe_in_seam.csv";
    std::string header = "timestamp";
    for (int i = 0; i < 108; ++i) header += ",c";
    std::ostringstream body;
    body << header << "\n";
    body << rawLine("t0", 100) << "\n";   // seed
    body << rawLine("t1", 101) << "\n";   // +1
    body << rawLine("t2", 104) << "\n";   // +3 (aggregated)
    writeFile(in, body.str());

    FakeEventStore fake;
    const long long n = mas::clean_file(in, fake);
    EXPECT_EQ(n, 2);
    ASSERT_EQ(fake.got.size(), 2u);
    EXPECT_EQ(fake.got[0].cap_seq, 101);
    EXPECT_EQ(fake.got[1].delta, 3);

    std::remove(in.c_str());
}

TEST(Pipeline, CleanFileWithStoreReturnsMinusOneOnMissingInput) {
    FakeEventStore fake;
    EXPECT_EQ(mas::clean_file("no_such_input_file.csv", fake), -1);
    EXPECT_TRUE(fake.got.empty());
}
```

Also add `#include <vector>` to the test file's includes if not present.

- [ ] **Step 4: Run — verify FAIL**

Run: `cmake --build build -j 2>&1 | tail -5`
Expected: **compile error** — no overload `clean_file(const std::string&, FakeEventStore&)`. (A compile error on the new test is the red state.)

- [ ] **Step 5: Extract `CsvEventStore` and add the overload**

`core/include/mas/CsvEventStore.hpp`:

```cpp
#pragma once
#include "mas/EventStore.hpp"
#include <fstream>
#include <string>

namespace mas {

// Appends cap events to a CSV file. Header row is written on construction.
class CsvEventStore : public IEventStore {
public:
    // Throws std::runtime_error if out_path cannot be created.
    CsvEventStore(const std::string& out_path, const std::string& machine_id);
    void write(std::span<const CapEvent> events) override;
    void close();   // flush; throws std::runtime_error on write error (e.g. disk full)

private:
    std::ofstream out_;
    std::string machine_id_;
};

} // namespace mas
```

`core/src/CsvEventStore.cpp`:

```cpp
#include "mas/CsvEventStore.hpp"
#include <stdexcept>

namespace mas {

CsvEventStore::CsvEventStore(const std::string& out_path, const std::string& machine_id)
    : out_(out_path), machine_id_(machine_id) {
    if (!out_.is_open())
        throw std::runtime_error("cannot create output file " + out_path);
    out_ << "machine_id,head_id,ts,cap_seq,app_torque,status,delta,is_fault,aggregated,reset\n";
}

void CsvEventStore::write(std::span<const CapEvent> events) {
    for (const auto& e : events) {
        out_ << machine_id_ << ',' << e.head_id << ',' << e.ts << ','
             << e.cap_seq << ',' << e.app_torque << ',' << e.status << ','
             << e.delta << ',' << (e.is_fault ? 1 : 0) << ','
             << (e.aggregated ? 1 : 0) << ',' << (e.reset ? 1 : 0) << '\n';
    }
}

void CsvEventStore::close() {
    out_.flush();
    if (out_.fail()) throw std::runtime_error("write error on output CSV");
}

} // namespace mas
```

Replace `core/include/mas/Pipeline.hpp` with:

```cpp
#pragma once
#include "mas/EventStore.hpp"
#include <string>

namespace mas {

// Read raw telemetry CSV at in_path, write every extracted cap event to
// `store` in batches. Returns the number of events written, or -1 if
// in_path cannot be opened. Store exceptions propagate to the caller.
long long clean_file(const std::string& in_path, IEventStore& store);

// CSV convenience wrapper (Plan-1 behavior): returns the event count;
// -1 if in_path cannot be opened; -2 if out_path cannot be created or a
// write fails.
long long clean_file(const std::string& in_path, const std::string& out_path,
                     const std::string& machine_id);

} // namespace mas
```

Replace `core/src/Pipeline.cpp` with:

```cpp
#include "mas/Pipeline.hpp"
#include "mas/CapEventExtractor.hpp"
#include "mas/CsvEventStore.hpp"
#include "mas/CsvRawReader.hpp"
#include <vector>

namespace mas {

long long clean_file(const std::string& in_path, IEventStore& store) {
    CsvRawReader reader(in_path);
    if (!reader.is_open()) return -1;   // input missing/unreadable

    constexpr std::size_t kBatch = 8192;   // spec §5.3: batched insert
    CapEventExtractor ex;
    RawRow row;
    std::vector<CapEvent> batch;
    long long n = 0;
    while (reader.next(row)) {
        ex.process(row, batch);
        if (batch.size() >= kBatch) {
            store.write(batch);
            n += static_cast<long long>(batch.size());
            batch.clear();
        }
    }
    store.write(batch);                 // final partial batch (may be empty)
    n += static_cast<long long>(batch.size());
    return n;
}

long long clean_file(const std::string& in_path, const std::string& out_path,
                     const std::string& machine_id) {
    {   // preserve Plan-1 semantics: missing input never creates the output file
        CsvRawReader probe(in_path);
        if (!probe.is_open()) return -1;
    }
    try {
        CsvEventStore store(out_path, machine_id);
        const long long n = clean_file(in_path, store);
        if (n < 0) return n;
        store.close();
        return n;
    } catch (const std::exception&) {
        return -2;
    }
}

} // namespace mas
```

Add the new source to `mas_core` in `CMakeLists.txt`:

```cmake
add_library(mas_core
  core/src/CapEventExtractor.cpp
  core/src/CsvRawReader.cpp
  core/src/CsvEventStore.cpp
  core/src/Pipeline.cpp)
```

- [ ] **Step 6: Run — verify PASS**

Run:
```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j && ctest --test-dir build --output-on-failure
```
Expected: **17/17 PASS** — 15 old (including both sentinel tests, unchanged) + 2 new seam tests.

- [ ] **Step 7: Commit**

```bash
git add CMakeLists.txt core/include/mas/EventStore.hpp core/include/mas/CsvEventStore.hpp core/src/CsvEventStore.cpp core/include/mas/Pipeline.hpp core/src/Pipeline.cpp tests/test_pipeline.cpp
git commit -m "refactor(core): IEventStore seam; extract CsvEventStore; batch writes (C++20)"
```

---

### Task 2: DuckDB dependency (pinned prebuilt) + smoke test

**Files:**
- Modify: `CMakeLists.txt`
- Test: `tests/test_duckdb_smoke.cpp`

**Interfaces:**
- Produces: CMake imported target **`duckdb_imported`** (header dir + shared lib) that Task 3 links against. No C++ interfaces.

- [ ] **Step 1: Write the failing smoke test**

`tests/test_duckdb_smoke.cpp`:

```cpp
#include <duckdb.hpp>
#include <gtest/gtest.h>

namespace {

TEST(DuckDbSmoke, OpensInMemoryAndSelects42) {
    duckdb::DuckDB db(nullptr);        // in-memory database
    duckdb::Connection con(db);
    auto res = con.Query("SELECT 42");
    ASSERT_FALSE(res->HasError()) << res->GetError();
    EXPECT_EQ(res->GetValue(0, 0).GetValue<int>(), 42);
}

} // namespace
```

Add it to `unit_tests` in `CMakeLists.txt`:

```cmake
add_executable(unit_tests
  tests/test_cap_event_extractor.cpp
  tests/test_csv_raw_reader.cpp
  tests/test_pipeline.cpp
  tests/test_duckdb_smoke.cpp)
```

- [ ] **Step 2: Run — verify FAIL**

Run: `cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j 2>&1 | tail -3`
Expected: **compile error** `'duckdb.hpp' file not found` (dependency not wired yet — this is red).

- [ ] **Step 3: Wire the prebuilt DuckDB via FetchContent**

In `CMakeLists.txt`, after the googletest `FetchContent_MakeAvailable` block, add:

```cmake
# DuckDB v1.2.2 prebuilt binary (never build from source — 30+ min).
if(APPLE)
  set(DUCKDB_ASSET libduckdb-osx-universal.zip)
  set(DUCKDB_LIBNAME libduckdb.dylib)
else()
  set(DUCKDB_ASSET libduckdb-linux-amd64.zip)
  set(DUCKDB_LIBNAME libduckdb.so)
endif()
FetchContent_Declare(duckdb_prebuilt
  URL https://github.com/duckdb/duckdb/releases/download/v1.2.2/${DUCKDB_ASSET})
FetchContent_MakeAvailable(duckdb_prebuilt)

add_library(duckdb_imported SHARED IMPORTED GLOBAL)
set_target_properties(duckdb_imported PROPERTIES
  IMPORTED_LOCATION "${duckdb_prebuilt_SOURCE_DIR}/${DUCKDB_LIBNAME}"
  INTERFACE_INCLUDE_DIRECTORIES "${duckdb_prebuilt_SOURCE_DIR}")
```

Then link the test binary and set the runtime search path (the prebuilt dylib's install name is `@rpath/libduckdb.dylib`). Extend the existing lines:

```cmake
target_link_libraries(unit_tests PRIVATE mas_core GTest::gtest_main duckdb_imported)
set_target_properties(unit_tests clean_exe PROPERTIES
  BUILD_RPATH "${duckdb_prebuilt_SOURCE_DIR}")
```

- [ ] **Step 4: Run — verify PASS**

Run:
```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release      # downloads ~50 MB once
cmake --build build -j
./build/unit_tests --gtest_filter='DuckDbSmoke.*'
```
Expected: `DuckDbSmoke.OpensInMemoryAndSelects42` **PASS**.

If the test binary fails to launch with `Library not loaded: @rpath/libduckdb.dylib`, inspect the install name and fix the rpath assumption:
```bash
otool -D build/_deps/duckdb_prebuilt-src/libduckdb.dylib
```
If the printed id is a bare `libduckdb.dylib` (no `@rpath/` prefix), rewrite it once:
```bash
install_name_tool -id @rpath/libduckdb.dylib build/_deps/duckdb_prebuilt-src/libduckdb.dylib
cmake --build build -j   # relink
```

- [ ] **Step 5: Run the full suite, then commit**

Run: `ctest --test-dir build --output-on-failure`
Expected: 18/18 PASS.

```bash
git add CMakeLists.txt tests/test_duckdb_smoke.cpp
git commit -m "build: DuckDB v1.2.2 prebuilt via FetchContent + smoke test"
```

---

### Task 3: `DuckDbEventStore` with idempotent upsert

**Files:**
- Create: `core/include/mas/DuckDbEventStore.hpp`, `core/src/DuckDbEventStore.cpp`
- Modify: `CMakeLists.txt`, `.gitignore`
- Test: `tests/test_duckdb_event_store.cpp`

**Interfaces:**
- Consumes: `IEventStore` (Task 1), `duckdb_imported` (Task 2).
- Produces: `class mas::DuckDbEventStore : public IEventStore` —
  - ctor `(const std::string& db_path, const std::string& machine_id)`, throws `std::runtime_error` on open failure; creates schema if absent.
  - `void write(std::span<const CapEvent> events) override` — idempotent: re-writing the same events never duplicates rows.
  - `long long count() const` — rows in `cap_events`.
  - `void export_parquet(const std::string& parquet_path)` — **declared here, implemented in Task 5.** Task 4's CLI uses ctor/`write`/`count` exactly as declared.

- [ ] **Step 1: Write the failing tests**

`tests/test_duckdb_event_store.cpp`:

```cpp
#include "mas/DuckDbEventStore.hpp"
#include <duckdb.hpp>
#include <gtest/gtest.h>
#include <cstdio>
#include <string>
#include <vector>

namespace {

mas::CapEvent ev(int head, const std::string& ts, long long seq, int delta = 1) {
    mas::CapEvent e;
    e.head_id = head;
    e.ts = ts;
    e.cap_seq = seq;
    e.app_torque = 2.0;
    e.status = 2.0;
    e.delta = delta;
    e.is_fault = false;
    e.aggregated = delta > 1;
    e.reset = false;
    return e;
}

void removeDb(const std::string& path) {
    std::remove(path.c_str());
    std::remove((path + ".wal").c_str());
}

TEST(DuckDbEventStore, WritePersistsRowsWithTypedColumns) {
    const std::string path = "t_store_basic.duckdb";
    removeDb(path);
    {
        mas::DuckDbEventStore store(path, "MCC1");
        std::vector<mas::CapEvent> batch = {
            ev(1, "2026-02-01T00:00:01.000", 101),
            ev(2, "2026-02-01T00:00:02.000", 55, 3),
        };
        store.write(batch);
        EXPECT_EQ(store.count(), 2);
    }   // store closed — safe to reopen the file directly
    duckdb::DuckDB db(path);
    duckdb::Connection con(db);
    auto res = con.Query(
        "SELECT machine_id, head_id, CAST(ts AS VARCHAR), cap_seq, delta, aggregated "
        "FROM cap_events WHERE head_id = 2");
    ASSERT_FALSE(res->HasError()) << res->GetError();
    ASSERT_EQ(res->RowCount(), 1u);
    EXPECT_EQ(res->GetValue(0, 0).GetValue<std::string>(), "MCC1");
    EXPECT_EQ(res->GetValue(1, 0).GetValue<int16_t>(), 2);
    EXPECT_EQ(res->GetValue(2, 0).GetValue<std::string>(), "2026-02-01 00:00:02");
    EXPECT_EQ(res->GetValue(3, 0).GetValue<int64_t>(), 55);
    EXPECT_EQ(res->GetValue(4, 0).GetValue<int32_t>(), 3);
    EXPECT_EQ(res->GetValue(5, 0).GetValue<bool>(), true);
    removeDb(path);
}

TEST(DuckDbEventStore, RewritingSameBatchIsIdempotent) {
    const std::string path = "t_store_idem.duckdb";
    removeDb(path);
    mas::DuckDbEventStore store(path, "MCC1");
    std::vector<mas::CapEvent> batch = {
        ev(1, "2026-02-01T00:00:01.000", 101),
        ev(1, "2026-02-01T00:00:05.000", 102),
    };
    store.write(batch);
    store.write(batch);                    // reprocess: must not duplicate (spec §10)
    EXPECT_EQ(store.count(), 2);
    removeDb(path);
}

TEST(DuckDbEventStore, DuplicateKeyWithinOneBatchIsIgnored) {
    const std::string path = "t_store_dup.duckdb";
    removeDb(path);
    mas::DuckDbEventStore store(path, "MCC1");
    std::vector<mas::CapEvent> batch = {
        ev(1, "2026-02-01T00:00:01.000", 101),
        ev(1, "2026-02-01T00:00:01.000", 101),   // same (machine, head, cap_seq)
    };
    store.write(batch);
    EXPECT_EQ(store.count(), 1);
    removeDb(path);
}

TEST(DuckDbEventStore, ReopenKeepsRowsAndUpsertsAcrossRuns) {
    const std::string path = "t_store_reopen.duckdb";
    removeDb(path);
    {
        mas::DuckDbEventStore store(path, "MCC1");
        std::vector<mas::CapEvent> b1 = {ev(1, "2026-02-01T00:00:01.000", 101)};
        store.write(b1);
    }
    {
        mas::DuckDbEventStore store(path, "MCC1");
        std::vector<mas::CapEvent> b2 = {
            ev(1, "2026-02-01T00:00:01.000", 101),   // already stored
            ev(1, "2026-02-01T00:00:09.000", 102),   // new
        };
        store.write(b2);
        EXPECT_EQ(store.count(), 2);
    }
    removeDb(path);
}

} // namespace
```

Add to `unit_tests` sources in `CMakeLists.txt`:

```cmake
add_executable(unit_tests
  tests/test_cap_event_extractor.cpp
  tests/test_csv_raw_reader.cpp
  tests/test_pipeline.cpp
  tests/test_duckdb_smoke.cpp
  tests/test_duckdb_event_store.cpp)
```

- [ ] **Step 2: Run — verify FAIL**

Run: `cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j 2>&1 | tail -3`
Expected: **compile error** `'mas/DuckDbEventStore.hpp' file not found` (red).

- [ ] **Step 3: Declare the store (pimpl — no duckdb.hpp in the header)**

`core/include/mas/DuckDbEventStore.hpp`:

```cpp
#pragma once
#include "mas/EventStore.hpp"
#include <memory>
#include <string>

namespace mas {

// DuckDB-backed cap_events store (spec §6 schema + is_reset column).
// Idempotent: UNIQUE(machine_id, head_id, cap_seq) + INSERT OR IGNORE,
// so reprocessing any day-file is safe (spec §10). Single-writer only
// (multi-process concurrency is spec §14 Q4, a later plan).
class DuckDbEventStore : public IEventStore {
public:
    // Opens/creates the database and the cap_events table.
    // Throws std::runtime_error if the database cannot be opened.
    DuckDbEventStore(const std::string& db_path, const std::string& machine_id);
    ~DuckDbEventStore() override;

    void write(std::span<const CapEvent> events) override;
    long long count() const;                             // rows in cap_events
    void export_parquet(const std::string& parquet_path); // implemented in Task 5

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace mas
```

- [ ] **Step 4: Implement the store**

`core/src/DuckDbEventStore.cpp`:

```cpp
#include "mas/DuckDbEventStore.hpp"
#include <duckdb.hpp>
#include <stdexcept>

namespace mas {

struct DuckDbEventStore::Impl {
    duckdb::DuckDB db;
    duckdb::Connection con;
    std::string machine_id;
    Impl(const std::string& path, std::string mid)
        : db(path), con(db), machine_id(std::move(mid)) {}
};

namespace {

void execOrThrow(duckdb::Connection& con, const std::string& sql) {
    auto res = con.Query(sql);
    if (res->HasError()) throw std::runtime_error(res->GetError());
}

} // namespace

DuckDbEventStore::DuckDbEventStore(const std::string& db_path,
                                   const std::string& machine_id) {
    try {
        impl_ = std::make_unique<Impl>(db_path, machine_id);
    } catch (const std::exception& e) {
        throw std::runtime_error("cannot open DuckDB database " + db_path + ": " + e.what());
    }
    // Spec §6 schema; is_reset added (Plan 1 folded ResetMarker into CapEvent).
    execOrThrow(impl_->con, R"sql(
        CREATE TABLE IF NOT EXISTS cap_events (
            machine_id VARCHAR NOT NULL,
            head_id    SMALLINT NOT NULL,
            ts         TIMESTAMP,
            cap_seq    BIGINT NOT NULL,
            app_torque REAL,
            status     REAL,
            delta      INTEGER,
            is_fault   BOOLEAN,
            aggregated BOOLEAN,
            is_reset   BOOLEAN,
            UNIQUE (machine_id, head_id, cap_seq)
        ))sql");
    // Staging table for batched appends; ts kept VARCHAR here, cast on merge.
    execOrThrow(impl_->con, R"sql(
        CREATE TABLE IF NOT EXISTS staging_cap_events (
            machine_id VARCHAR, head_id SMALLINT, ts VARCHAR,
            cap_seq BIGINT, app_torque REAL, status REAL,
            delta INTEGER, is_fault BOOLEAN, aggregated BOOLEAN, is_reset BOOLEAN
        ))sql");
    execOrThrow(impl_->con, "DELETE FROM staging_cap_events");  // stale rows from a crashed run
}

DuckDbEventStore::~DuckDbEventStore() = default;

void DuckDbEventStore::write(std::span<const CapEvent> events) {
    if (events.empty()) return;
    {
        duckdb::Appender app(impl_->con, "staging_cap_events");
        for (const auto& e : events) {
            app.BeginRow();
            app.Append(duckdb::Value(impl_->machine_id));
            app.Append(duckdb::Value::SMALLINT(static_cast<int16_t>(e.head_id)));
            app.Append(duckdb::Value(e.ts));
            app.Append(duckdb::Value::BIGINT(e.cap_seq));
            app.Append(duckdb::Value::FLOAT(static_cast<float>(e.app_torque)));
            app.Append(duckdb::Value::FLOAT(static_cast<float>(e.status)));
            app.Append(duckdb::Value::INTEGER(e.delta));
            app.Append(duckdb::Value::BOOLEAN(e.is_fault));
            app.Append(duckdb::Value::BOOLEAN(e.aggregated));
            app.Append(duckdb::Value::BOOLEAN(e.reset));
            app.EndRow();
        }
        app.Close();
    }
    // Idempotent merge: UNIQUE key drops rows already in cap_events.
    execOrThrow(impl_->con, R"sql(
        INSERT OR IGNORE INTO cap_events
        SELECT machine_id, head_id, CAST(ts AS TIMESTAMP), cap_seq, app_torque,
               status, delta, is_fault, aggregated, is_reset
        FROM staging_cap_events)sql");
    execOrThrow(impl_->con, "DELETE FROM staging_cap_events");
}

long long DuckDbEventStore::count() const {
    auto res = impl_->con.Query("SELECT COUNT(*) FROM cap_events");
    if (res->HasError()) throw std::runtime_error(res->GetError());
    return res->GetValue(0, 0).GetValue<int64_t>();
}

void DuckDbEventStore::export_parquet(const std::string&) {
    throw std::runtime_error("export_parquet: implemented in Task 5");
}

} // namespace mas
```

Note on `DuplicateKeyWithinOneBatchIsIgnored`: `INSERT OR IGNORE ... SELECT` must also swallow duplicates *inside* the staging batch. If DuckDB v1.2.2 rejects within-statement duplicates (error mentioning the UNIQUE constraint on the same statement), change the merge SELECT to pre-deduplicate:

```sql
INSERT OR IGNORE INTO cap_events
SELECT machine_id, head_id, CAST(ts AS TIMESTAMP), cap_seq, app_torque,
       status, delta, is_fault, aggregated, is_reset
FROM (SELECT *, row_number() OVER (PARTITION BY machine_id, head_id, cap_seq) AS rn
      FROM staging_cap_events) WHERE rn = 1
```

- [ ] **Step 5: Wire the build and ignore WAL files**

In `CMakeLists.txt`:

```cmake
add_library(mas_core
  core/src/CapEventExtractor.cpp
  core/src/CsvRawReader.cpp
  core/src/CsvEventStore.cpp
  core/src/DuckDbEventStore.cpp
  core/src/Pipeline.cpp)
target_include_directories(mas_core PUBLIC core/include)
target_link_libraries(mas_core PRIVATE duckdb_imported)
```

(`PRIVATE`: the pimpl keeps `duckdb.hpp` out of public headers, so dependents don't need DuckDB include dirs from `mas_core`. `unit_tests` links `duckdb_imported` directly already — Task 2.)

Append to `.gitignore`:

```
*.wal
```

- [ ] **Step 6: Run — verify PASS**

Run:
```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j && ctest --test-dir build --output-on-failure
```
Expected: **22/22 PASS** (18 previous + 4 new store tests; the parquet stub isn't tested yet).

- [ ] **Step 7: Commit**

```bash
git add CMakeLists.txt .gitignore core/include/mas/DuckDbEventStore.hpp core/src/DuckDbEventStore.cpp tests/test_duckdb_event_store.cpp
git commit -m "feat(store): DuckDB cap_events store with idempotent upsert on (machine,head,cap_seq)"
```

---

### Task 4: Pipeline→DuckDB end-to-end + `clean` CLI dispatch

**Files:**
- Modify: `core/src/clean_main.cpp`
- Test: `tests/test_pipeline.cpp`

**Interfaces:**
- Consumes: `clean_file(in, IEventStore&)` (Task 1), `DuckDbEventStore` (Task 3).
- Produces: `clean <raw_in.csv> <out.csv|out.duckdb> [machine_id]` — output ending in `.duckdb` selects the DuckDB store; anything else keeps the Plan-1 CSV path. Exit codes unchanged: 0 ok, 1 I/O error, 2 usage.

- [ ] **Step 1: Write the failing end-to-end test**

Append to `tests/test_pipeline.cpp` (inside the anonymous namespace), and add `#include "mas/DuckDbEventStore.hpp"` at the top:

```cpp
TEST(Pipeline, CleanFileIntoDuckDbTwiceIsIdempotent) {
    const std::string in = "pipe_in_ddb.csv", db = "pipe_out_ddb.duckdb";
    std::remove(db.c_str());
    std::remove((db + ".wal").c_str());

    std::string header = "timestamp";
    for (int i = 0; i < 108; ++i) header += ",c";
    std::ostringstream body;
    body << header << "\n";
    body << rawLine("2026-02-01T00:00:00.000", 100) << "\n";   // seed
    body << rawLine("2026-02-01T00:00:01.000", 101) << "\n";   // +1
    body << rawLine("2026-02-01T00:00:02.000", 104) << "\n";   // +3
    writeFile(in, body.str());

    mas::DuckDbEventStore store(db, "MCCtest");
    EXPECT_EQ(mas::clean_file(in, store), 2);
    EXPECT_EQ(store.count(), 2);
    EXPECT_EQ(mas::clean_file(in, store), 2);   // fresh extractor emits the same 2 events
    EXPECT_EQ(store.count(), 2);                // upsert drops them — reprocessing is safe

    std::remove(in.c_str());
    std::remove(db.c_str());
    std::remove((db + ".wal").c_str());
}
```

- [ ] **Step 2: Run — verify FAIL, then PASS**

Run: `cmake --build build -j && ./build/unit_tests --gtest_filter='*DuckDbTwice*'`
Expected: **PASS immediately** — Tasks 1+3 already provide everything. This test is the integration gate, not new logic; if it fails, Task 3's merge SQL or the ts cast is broken — fix there, not here. (Timestamps here use the real `2026-02-01T00:00:00.000` format on purpose: `"t0"` would fail the `CAST(ts AS TIMESTAMP)`.)

- [ ] **Step 3: Add the CLI dispatch**

Replace `core/src/clean_main.cpp` with:

```cpp
#include "mas/DuckDbEventStore.hpp"
#include "mas/Pipeline.hpp"
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    if (argc < 3) {
        std::cerr << "usage: clean <raw_in.csv> <events_out.csv|events_out.duckdb> [machine_id]\n";
        return 2;
    }
    const std::string in = argv[1], out = argv[2];
    const std::string machine = (argc > 3) ? argv[3] : "MCC";

    if (std::string_view(out).ends_with(".duckdb")) {
        try {
            mas::DuckDbEventStore store(out, machine);
            const long long n = mas::clean_file(in, store);
            if (n == -1) {
                std::cerr << "error: cannot open input file " << in << "\n";
                return 1;
            }
            std::cerr << "wrote " << n << " cap events; store now holds "
                      << store.count() << " rows\n";
        } catch (const std::exception& e) {
            std::cerr << "error: " << e.what() << "\n";
            return 1;
        }
        return 0;
    }

    const long long n = mas::clean_file(in, out, machine);
    if (n == -1) {
        std::cerr << "error: cannot open input file " << in << "\n";
        return 1;
    }
    if (n < 0) {
        std::cerr << "error: cannot write output file " << out << "\n";
        return 1;
    }
    std::cerr << "wrote " << n << " cap events\n";
    return 0;
}
```

(Add `#include <string_view>` if the compiler complains.)

- [ ] **Step 4: Build, run the suite, and exercise the CLI manually**

Run:
```bash
cmake --build build -j && ctest --test-dir build --output-on-failure
python3 - <<'EOF'
rows = [("2026-02-01T00:00:00.000",100),("2026-02-01T00:00:01.000",101),("2026-02-01T00:00:02.000",104)]
hdr = "timestamp," + ",".join(f"c{i}" for i in range(108))
with open("/tmp/mini_raw.csv","w") as f:
    f.write(hdr+"\n")
    for ts,c in rows:
        f.write(ts + f",{c}.0" + ",0.0"*35 + ",2.0" + ",0.0"*35 + ",2.0" + ",0.0"*35 + "\n")
EOF
./build/clean /tmp/mini_raw.csv /tmp/mini_events.duckdb MCCdemo
./build/clean /tmp/mini_raw.csv /tmp/mini_events.duckdb MCCdemo
rm -f /tmp/mini_raw.csv /tmp/mini_events.duckdb /tmp/mini_events.duckdb.wal
```
Expected: 23/23 tests PASS; the CLI prints `wrote 2 cap events; store now holds 2 rows` **both times** (second run proves idempotency at the CLI level).

- [ ] **Step 5: Commit**

```bash
git add core/src/clean_main.cpp tests/test_pipeline.cpp
git commit -m "feat(cli): clean writes to DuckDB when output ends in .duckdb; e2e idempotency test"
```

---

### Task 5: Parquet export

**Files:**
- Modify: `core/src/DuckDbEventStore.cpp`
- Test: `tests/test_duckdb_event_store.cpp`

**Interfaces:**
- Consumes: `DuckDbEventStore` (Task 3 — the method is already declared).
- Produces: `void DuckDbEventStore::export_parquet(const std::string& parquet_path)` — writes all of `cap_events` (sorted `head_id, ts`, spec §6) to a Parquet file; throws `std::runtime_error` on failure.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_duckdb_event_store.cpp` (inside the anonymous namespace):

```cpp
TEST(DuckDbEventStore, ExportParquetRoundtrips) {
    const std::string path = "t_store_parquet.duckdb";
    const std::string pq = "t_store_events.parquet";
    removeDb(path);
    std::remove(pq.c_str());

    mas::DuckDbEventStore store(path, "MCC1");
    std::vector<mas::CapEvent> batch = {
        ev(1, "2026-02-01T00:00:01.000", 101),
        ev(1, "2026-02-01T00:00:05.000", 102),
        ev(7, "2026-02-01T00:00:03.000", 900, 2),
    };
    store.write(batch);
    store.export_parquet(pq);

    duckdb::DuckDB mem(nullptr);
    duckdb::Connection con(mem);
    auto res = con.Query("SELECT COUNT(*), MIN(head_id), MAX(cap_seq) FROM read_parquet('" + pq + "')");
    ASSERT_FALSE(res->HasError()) << res->GetError();
    EXPECT_EQ(res->GetValue(0, 0).GetValue<int64_t>(), 3);
    EXPECT_EQ(res->GetValue(1, 0).GetValue<int16_t>(), 1);
    EXPECT_EQ(res->GetValue(2, 0).GetValue<int64_t>(), 900);

    std::remove(pq.c_str());
    removeDb(path);
}
```

- [ ] **Step 2: Run — verify FAIL**

Run: `cmake --build build -j && ./build/unit_tests --gtest_filter='*Parquet*'`
Expected: FAIL — the Task-3 stub throws `export_parquet: implemented in Task 5`.

- [ ] **Step 3: Implement**

In `core/src/DuckDbEventStore.cpp`, replace the `export_parquet` stub with:

```cpp
void DuckDbEventStore::export_parquet(const std::string& parquet_path) {
    // Note: path is spliced into SQL — fine for trusted local paths, but a
    // path containing a single quote would break the statement.
    execOrThrow(impl_->con,
        "COPY (SELECT * FROM cap_events ORDER BY head_id, ts) TO '" +
        parquet_path + "' (FORMAT PARQUET)");
}
```

- [ ] **Step 4: Run — verify PASS**

Run: `cmake --build build -j && ctest --test-dir build --output-on-failure`
Expected: **24/24 PASS** (the prebuilt libduckdb bundles the Parquet extension statically).

- [ ] **Step 5: Commit**

```bash
git add core/src/DuckDbEventStore.cpp tests/test_duckdb_event_store.cpp
git commit -m "feat(store): Parquet export of cap_events (sorted head_id, ts)"
```

---

### Task 6: Real-data validation — idempotent reprocessing at full scale

**Files:**
- Create: `docs/validation-log.md`

**Interfaces:**
- Consumes: `clean` CLI (Task 4), Python oracle (Plan 1). Nothing produced for later tasks — this is the evidence-gathering gate (spec §10, §11).

- [ ] **Step 1: Extract one real day locally (never into git)**

```bash
unzip -o telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02.zip \
  telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02-01.csv -d /tmp/
```

- [ ] **Step 2: Clean into DuckDB twice; verify counts**

```bash
./build/clean /tmp/telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02-01.csv /tmp/real_events.duckdb MCC777eda3db57348ef8a3113a642ae74db
./build/clean /tmp/telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02-01.csv /tmp/real_events.duckdb MCC777eda3db57348ef8a3113a642ae74db
```
Expected: **both** runs print `wrote 765711 cap events; store now holds 765711 rows`. Run 1 inserts 765,711 rows (matches Plan 1's oracle/CSV number); run 2 emits the same events and the upsert drops every one — store row count unchanged. This is spec §10's "reprocessing any day is safe" demonstrated on real data.

- [ ] **Step 3: Cross-check against the Python oracle (unchanged from Plan 1)**

```bash
cd python && python3 -c "
import oracle
print(len(oracle.extract('/tmp/telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02-01.csv')))" && cd ..
```
Expected: `765711` — same as the DuckDB row count.

- [ ] **Step 4: Record the evidence**

Create `docs/validation-log.md`:

```markdown
# Validation Log

## 2026-07-06 — Plan 2: DuckDB store, idempotent reprocessing (real data)

- File: telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02-01.csv (86,399 rows, 109 cols)
- `clean` → DuckDB run 1: wrote 765,711 events; store rows 765,711
- `clean` → DuckDB run 2 (same file): wrote 765,711 events; store rows **765,711** (unchanged — upsert)
- Python oracle events: 765,711 (match)
- Spec cross-refs: §6 schema + UNIQUE key, §10 idempotency backbone, ~765k/day Appendix A
```

(If the measured numbers differ, record the actual ones and investigate before committing — a mismatch with Plan 1's 765,711 means a regression in the extractor or reader path, since the dedup logic did not change in this plan.)

- [ ] **Step 5: Clean up and commit**

```bash
rm -f /tmp/telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02-01.csv /tmp/real_events.duckdb /tmp/real_events.duckdb.wal
git add docs/validation-log.md
git commit -m "docs: real-data validation log — DuckDB idempotent reprocessing, 765,711 events"
```

---

## Self-Review

**Spec coverage (Plan 2 scope = spec §6 store schema/upsert + §10 idempotency backbone + §12 DuckDB/Parquet persistence):**
- `IEventStore` seam, spec §7 signature `write(std::span<const CapEvent>)` → Task 1. ✓
- Batched insert (spec §5.3 Event Writer) → Task 1 (`kBatch = 8192` in `clean_file`). ✓
- DuckDB embedded store (spec §12) → Tasks 2–3. ✓
- `cap_events` schema with exact spec §6 columns + documented `is_reset` extension → Task 3. ✓
- `UNIQUE (machine_id, head_id, cap_seq)` + idempotent upsert (spec §6, §10) → Task 3, tested at unit (Task 3), pipeline (Task 4), CLI/real-data (Task 6) levels. ✓
- Parquet for the cleaned store (spec §12), sorted `(head_id, ts)` (spec §6) → Task 5. ✓
- DIP: no public header includes `duckdb.hpp` (pimpl); domain untouched (spec §7) → Task 3. ✓
- Real-data cross-check vs ~765k/day and oracle (spec §11) → Task 6. ✓
- Deliberately out of scope, noted in Global Constraints: partitioned Parquet layout per `(machine_id, date(ts))` (needs the multi-day ingestion plan), raw store, multi-writer concurrency (spec §14 Q4), Results Store. Not gaps.

**Placeholder scan:** the only "implemented later" is Task 3's `export_parquet` stub, which throws loudly, is declared with its final signature, and is delivered inside this same plan (Task 5) with a red test proving the stub throws. No TBD/TODO elsewhere; every step has concrete code or exact commands. ✓

**Type consistency:** `IEventStore::write(std::span<const CapEvent>)` is identical in Tasks 1/3 and both call sites (`Pipeline.cpp`, fake in tests). `CsvEventStore(out_path, machine_id)` / `close()`, `DuckDbEventStore(db_path, machine_id)` / `write` / `count()` / `export_parquet(parquet_path)` match everywhere they appear (Tasks 3–6). `clean_file(in, IEventStore&)` vs `clean_file(in, out, machine_id)` overloads used consistently (Tasks 1, 4, CLI). DB column `is_reset` used in both CREATE TABLE statements and the merge SELECT; C++ field stays `e.reset`; CSV header stays `reset`. Sentinels `-1`/`-2` preserved with the probe-open trick. ✓
