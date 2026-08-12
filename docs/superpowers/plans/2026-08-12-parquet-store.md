# Parquet Event Store Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A second storage backend with no index, no WAL and no constraint, and a measurement of whether removing them is worth what it costs.

**Architecture:** `ParquetEventStore` implements the existing two-method `IEventStore`, buffering one day-file's events and writing them as a single Parquet on close. The analytics tier reads a directory of them through a view named `cap_events`, so none of the eight tools change. Idempotency moves from a UNIQUE constraint to the filename; re-dispatch duplicates are removed by a `DISTINCT ON` in that view.

**Tech Stack:** C++20, DuckDB (already present — used in-memory as the Parquet writer, no Arrow dependency added), Python 3 + pytest, the existing `bench/run_bench.sh` harness.

**Spec:** `docs/superpowers/specs/2026-08-12-parquet-store-design.md`

## Global Constraints

- **DuckDB stays the default.** `--format` defaults to `duckdb`; every existing command must behave exactly as it does today when the flag is absent. 85 C++ tests and 230 Python tests stay green throughout (225 pass, 5 skip without the rebuilt store).
- **No tool changes.** The eight files in `python/analytics/tools/` contain 15 occurrences of `FROM cap_events` and none may be edited. That property is what makes the comparison honest: same SQL, two backends.
- **The benchmark must be able to say Parquet loses.** Spec §9.5. A comparison with one possible outcome is not a comparison.
- Event schema is unchanged: `machine_id, head_id, ts, cap_seq, app_torque, status, delta, is_fault, aggregated, is_reset`. Ten columns, same order, same types as `DuckDbEventStore`'s table.
- Paths are spliced into SQL and must go through `sql_quote` (already in `core/src/store/DuckDbEventStore.cpp`). A path containing `'` must not truncate the statement.
- Test commands: `./build/unit_tests` from the repo root; `cd python && ../.venv/bin/python -m pytest -q`.
- Build: `cmake --build build --parallel`. Never rebuild while a benchmark sweep is running.

---

## File Structure

| File | Responsibility |
|---|---|
| `core/include/mas/store/ParquetEventStore.hpp` | The store's interface |
| `core/src/store/ParquetEventStore.cpp` | Buffer + one `COPY ... TO ... (FORMAT PARQUET)` on close |
| `core/include/mas/store/SqlQuote.hpp` | `sql_quote`, lifted out of `DuckDbEventStore.cpp` so both stores share one escape |
| `tests/test_parquet_event_store.cpp` | T1–T5 |
| `core/src/apps/clean_main.cpp` | `--format parquet` |
| `core/src/apps/monolith_main.cpp` | `--format parquet`, store constructed per input file |
| `core/src/apps/worker_main.cpp` | `--format parquet` |
| `python/analytics/store.py` | `connect()` returns a view over Parquet when the path is a directory |
| `python/tests/conftest.py` | `tiny_store_parquet` fixture beside `tiny_store` |
| `python/tests/test_backend_parity.py` | T6 — the suite's expectations, run on both backends |
| `bench/run_bench.sh` | `parquet` arch |
| `bench/read_bench.py` | Read-side timing for the three report types |

---

## Task 1: `sql_quote` becomes shared

Both stores splice paths into SQL. Lift the existing helper before a second copy exists.

**Files:**
- Create: `core/include/mas/store/SqlQuote.hpp`
- Modify: `core/src/store/DuckDbEventStore.cpp` (remove the local copy, include the header)

**Interfaces:**
- Produces: `mas::sql_quote(const std::string&) -> std::string` — used by Task 2

- [ ] **Step 1: Create the header**

```cpp
#pragma once
#include <string>

namespace mas {

// ATTACH, COPY and read_parquet all take a path as a SQL string literal, and
// DuckDB has no parameter binding for any of them. Doubling embedded quotes is
// the escape SQL defines; without it a path like /tmp/o'brien/store.duckdb
// terminates the literal early and the statement fails with a parse error
// nobody can read.
inline std::string sql_quote(const std::string& s) {
    std::string out;
    out.reserve(s.size());
    for (const char c : s) {
        out.push_back(c);
        if (c == '\'') out.push_back('\'');
    }
    return out;
}

} // namespace mas
```

- [ ] **Step 2: Remove the local copy from `DuckDbEventStore.cpp`**

Delete the `std::string sql_quote(const std::string& s) { ... }` definition and its
comment from the anonymous namespace, and add to the includes:

```cpp
#include "mas/store/SqlQuote.hpp"
```

- [ ] **Step 3: Build and run the suite**

Run: `cmake --build build --parallel && ./build/unit_tests 2>&1 | tail -3`
Expected: `[  PASSED  ] 85 tests.` — pure move, no behaviour change. The existing
`DuckDbEventStore.PathContainingASingleQuoteWorks` test proves the move was clean.

- [ ] **Step 4: Commit**

```bash
git add core/include/mas/store/SqlQuote.hpp core/src/store/DuckDbEventStore.cpp
git commit -m "refactor(store): share sql_quote before a second store needs it"
```

---

## Task 2: `ParquetEventStore`

**Files:**
- Create: `core/include/mas/store/ParquetEventStore.hpp`
- Create: `core/src/store/ParquetEventStore.cpp`
- Create: `tests/test_parquet_event_store.cpp`
- Modify: `CMakeLists.txt` (add the source to `mas_store`, the test to `unit_tests`)

**Interfaces:**
- Consumes: `mas::sql_quote` (Task 1), `mas::IEventStore`, `mas::CapEvent`
- Produces: `mas::ParquetEventStore(out_path, machine_id)`, `.write(span)`, `.close()`, `.count()` — used by Tasks 3 and 4

- [ ] **Step 1: Write the failing tests**

Create `tests/test_parquet_event_store.cpp`:

```cpp
#include "mas/store/ParquetEventStore.hpp"
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

// Count rows a glob resolves to, through the same read path the analytics uses.
long long rowsIn(const std::string& glob) {
    duckdb::DuckDB mem(nullptr);
    duckdb::Connection con(mem);
    auto res = con.Query("SELECT COUNT(*) FROM read_parquet('" + glob + "')");
    EXPECT_FALSE(res->HasError()) << res->GetError();
    return res->GetValue(0, 0).GetValue<int64_t>();
}

} // namespace

TEST(ParquetEventStore, RoundTripsEveryColumn) {
    const std::string p = "t_pq_roundtrip.parquet";
    std::remove(p.c_str());
    {
        mas::ParquetEventStore s(p, "MCC1");
        std::vector<mas::CapEvent> b = {ev(1, "2026-02-01T00:00:01.000", 101),
                                        ev(7, "2026-02-01T00:00:03.000", 900, 3)};
        b[1].app_torque = 1.997;
        b[1].status = 65.0;
        b[1].is_fault = true;
        s.write(b);
        s.close();
    }
    duckdb::DuckDB mem(nullptr);
    duckdb::Connection con(mem);
    auto res = con.Query(
        "SELECT machine_id, head_id, CAST(ts AS VARCHAR), cap_seq, app_torque, "
        "status, delta, is_fault, aggregated, is_reset "
        "FROM read_parquet('" + p + "') WHERE head_id = 7");
    ASSERT_FALSE(res->HasError()) << res->GetError();
    ASSERT_EQ(res->RowCount(), 1u);
    EXPECT_EQ(res->GetValue(0, 0).GetValue<std::string>(), "MCC1");
    EXPECT_EQ(res->GetValue(1, 0).GetValue<int16_t>(), 7);
    EXPECT_EQ(res->GetValue(2, 0).GetValue<std::string>(), "2026-02-01 00:00:03");
    EXPECT_EQ(res->GetValue(3, 0).GetValue<int64_t>(), 900);
    EXPECT_FLOAT_EQ(res->GetValue(4, 0).GetValue<float>(), 1.997f);
    EXPECT_FLOAT_EQ(res->GetValue(5, 0).GetValue<float>(), 65.0f);
    EXPECT_EQ(res->GetValue(6, 0).GetValue<int32_t>(), 3);
    EXPECT_TRUE(res->GetValue(7, 0).GetValue<bool>());
    EXPECT_TRUE(res->GetValue(8, 0).GetValue<bool>());
    EXPECT_FALSE(res->GetValue(9, 0).GetValue<bool>());
    std::remove(p.c_str());
}

TEST(ParquetEventStore, ReprocessingOverwritesTheSameFile) {
    // Idempotency here is a property of the filename, not of a constraint:
    // the same input produces the same path, and the second run replaces it.
    const std::string p = "t_pq_idem.parquet";
    std::remove(p.c_str());
    std::vector<mas::CapEvent> b = {ev(1, "2026-02-01T00:00:01.000", 101),
                                    ev(1, "2026-02-01T00:00:02.000", 102)};
    for (int run = 0; run < 2; ++run) {
        mas::ParquetEventStore s(p, "MCC1");
        s.write(b);
        s.close();
    }
    EXPECT_EQ(rowsIn(p), 2);
    std::remove(p.c_str());
}

TEST(ParquetEventStore, EmptyInputWritesAReadableFile) {
    // A day-file that yields no events must still leave something a glob can
    // read, or every later read_parquet('*.parquet') fails on its account.
    const std::string p = "t_pq_empty.parquet";
    std::remove(p.c_str());
    {
        mas::ParquetEventStore s(p, "MCC1");
        s.close();
    }
    EXPECT_EQ(rowsIn(p), 0);
    std::remove(p.c_str());
}

TEST(ParquetEventStore, PathContainingASingleQuoteWorks) {
    const std::string p = "t_pq_o'brien.parquet";
    std::remove(p.c_str());
    {
        mas::ParquetEventStore s(p, "MCC1");
        std::vector<mas::CapEvent> b = {ev(1, "2026-02-01T00:00:01.000", 101)};
        s.write(b);
        EXPECT_NO_THROW(s.close());
    }
    EXPECT_EQ(rowsIn("t_pq_o''brien.parquet"), 1);
    std::remove(p.c_str());
}

TEST(ParquetEventStore, CountReportsEventsAccepted) {
    const std::string p = "t_pq_count.parquet";
    std::remove(p.c_str());
    mas::ParquetEventStore s(p, "MCC1");
    std::vector<mas::CapEvent> b = {ev(1, "2026-02-01T00:00:01.000", 101),
                                    ev(1, "2026-02-01T00:00:02.000", 102)};
    s.write(b);
    s.write(b);                       // buffered, not deduplicated: no constraint here
    EXPECT_EQ(s.count(), 4);
    s.close();
    std::remove(p.c_str());
}
```

- [ ] **Step 2: Wire into CMake, then run to see it fail**

In `CMakeLists.txt`, add to `mas_store`'s sources:

```cmake
  core/src/store/ParquetEventStore.cpp
```

and to `MAS_TEST_SOURCES` inside the `if(NOT MAS_BENCH_ONLY)` branch (it needs DuckDB):

```cmake
    tests/test_parquet_event_store.cpp
```

Run: `cmake -S . -B build && cmake --build build --parallel 2>&1 | tail -5`
Expected: FAIL — `fatal error: 'mas/store/ParquetEventStore.hpp' file not found`.

- [ ] **Step 3: Write the header**

Create `core/include/mas/store/ParquetEventStore.hpp`:

```cpp
#pragma once
#include "mas/store/EventStore.hpp"
#include <memory>
#include <span>
#include <string>

namespace mas {

// One Parquet file per input day-file: no index, no write-ahead log, no
// constraint. That is the point — this store exists so the project can measure
// what those cost in DuckDbEventStore, where persistence is 79.8% of
// end-to-end wall-clock (docs/bench/results.md).
//
// Idempotency is a property of the filename rather than of a UNIQUE key:
// reprocessing an input writes the same path and replaces it. Duplicates from
// a re-dispatched work item land in two differently-named files and are
// removed by the reader's view (spec §3).
class ParquetEventStore : public IEventStore {
public:
    // Throws std::runtime_error if the parent directory does not exist and
    // cannot be created.
    ParquetEventStore(const std::string& out_path, const std::string& machine_id);
    ~ParquetEventStore() override;   // calls close(), logs and swallows failure

    void write(std::span<const CapEvent> events) override;   // buffers
    void close();                    // writes the file; throws on failure
    long long count() const;         // events accepted so far

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace mas
```

- [ ] **Step 4: Write the implementation**

Create `core/src/store/ParquetEventStore.cpp`:

```cpp
#include "mas/store/ParquetEventStore.hpp"
#include "mas/store/SqlQuote.hpp"
#include <duckdb.hpp>
#include <filesystem>
#include <iostream>
#include <stdexcept>

namespace mas {

struct ParquetEventStore::Impl {
    duckdb::DuckDB db;          // in-memory: no file, no WAL, no checkpoint
    duckdb::Connection con;
    std::string path;
    std::string machine_id;
    long long n = 0;
    bool closed = false;
    Impl(std::string p, std::string mid)
        : db(nullptr), con(db), path(std::move(p)), machine_id(std::move(mid)) {}
};

namespace {

void execOrThrow(duckdb::Connection& con, const std::string& sql) {
    auto res = con.Query(sql);
    if (res->HasError()) throw std::runtime_error(res->GetError());
}

} // namespace

ParquetEventStore::ParquetEventStore(const std::string& out_path,
                                     const std::string& machine_id) {
    const auto parent = std::filesystem::path(out_path).parent_path();
    if (!parent.empty()) {
        std::error_code ec;
        std::filesystem::create_directories(parent, ec);
        if (ec && !std::filesystem::is_directory(parent))
            throw std::runtime_error("cannot create directory " + parent.string() +
                                     ": " + ec.message());
    }
    impl_ = std::make_unique<Impl>(out_path, machine_id);
    // Same ten columns and types as cap_events, minus the constraint. The
    // absence of UNIQUE is the whole experiment.
    execOrThrow(impl_->con, R"sql(
        CREATE TABLE buf (
            machine_id VARCHAR NOT NULL,
            head_id    SMALLINT NOT NULL,
            ts         TIMESTAMP NOT NULL,
            cap_seq    BIGINT NOT NULL,
            app_torque REAL,
            status     REAL,
            delta      INTEGER,
            is_fault   BOOLEAN,
            aggregated BOOLEAN,
            is_reset   BOOLEAN
        ))sql");
}

ParquetEventStore::~ParquetEventStore() {
    try {
        close();
    } catch (const std::exception& e) {
        // A destructor cannot report failure, and throwing from one during
        // stack unwinding terminates. Say it loudly instead; callers that need
        // the error call close() themselves.
        std::cerr << "error: writing " << impl_->path << ": " << e.what() << "\n";
    }
}

void ParquetEventStore::write(std::span<const CapEvent> events) {
    if (events.empty()) return;
    duckdb::Appender app(impl_->con, "buf");
    for (const auto& e : events) {
        app.BeginRow();
        app.Append(duckdb::Value(impl_->machine_id));
        app.Append(duckdb::Value::SMALLINT(static_cast<int16_t>(e.head_id)));
        app.Append(duckdb::Value(e.ts));              // VARCHAR -> TIMESTAMP cast
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
    impl_->n += static_cast<long long>(events.size());
}

void ParquetEventStore::close() {
    if (impl_->closed) return;
    impl_->closed = true;
    // Written even when empty: a day-file that yields no events must still
    // leave a file with the right schema, or a later read_parquet over the
    // directory fails because of it.
    execOrThrow(impl_->con,
        "COPY (SELECT * FROM buf ORDER BY head_id, ts) TO '" +
        sql_quote(impl_->path) + "' (FORMAT PARQUET)");
}

long long ParquetEventStore::count() const { return impl_->n; }

} // namespace mas
```

- [ ] **Step 5: Run the tests**

Run: `cmake --build build --parallel && ./build/unit_tests --gtest_filter='ParquetEventStore.*'`
Expected: PASS, 5 tests.

- [ ] **Step 6: Confirm nothing regressed**

Run: `./build/unit_tests 2>&1 | tail -3`
Expected: `[  PASSED  ] 90 tests.` (85 + 5)

- [ ] **Step 7: Commit**

```bash
git add core/include/mas/store/ParquetEventStore.hpp core/src/store/ParquetEventStore.cpp \
        tests/test_parquet_event_store.cpp CMakeLists.txt
git commit -m "feat(store): Parquet backend with no index, WAL or constraint

Idempotency comes from the filename instead: reprocessing an input writes the
same path and replaces it. Duplicates from a re-dispatched item land in two
files and are the reader's problem, by design."
```

---

## Task 3: `--format parquet` in the three apps

**Files:**
- Modify: `core/src/apps/clean_main.cpp`, `core/src/apps/monolith_main.cpp`, `core/src/apps/worker_main.cpp`

**Interfaces:**
- Consumes: `mas::ParquetEventStore` (Task 2)
- Produces: `<binary> --format parquet <out-dir> ...` writing `<out-dir>/<input-basename>.parquet`

- [ ] **Step 1: Add a shared helper for the output path**

Append to `core/include/mas/store/ParquetEventStore.hpp`, inside `namespace mas`:

```cpp
// <dir>/<input basename without extension>.parquet — the naming that makes
// reprocessing idempotent.
std::string parquet_path_for(const std::string& out_dir, const std::string& in_path);
```

and to `core/src/store/ParquetEventStore.cpp`:

```cpp
std::string parquet_path_for(const std::string& out_dir, const std::string& in_path) {
    return (std::filesystem::path(out_dir) /
            (std::filesystem::path(in_path).stem().string() + ".parquet")).string();
}
```

- [ ] **Step 2: `clean_main.cpp`**

After `const std::string machine = (argc > 3) ? argv[3] : "MCC";`, replace the
argument handling so a leading `--format parquet` is consumed:

```cpp
    int argi = 1;
    bool parquet = false;
    if (std::string(argv[argi]) == "--format") {
        if (argc < argi + 2) { std::cerr << "error: --format needs a value\n"; return 2; }
        const std::string fmt = argv[argi + 1];
        if (fmt == "parquet") parquet = true;
        else if (fmt != "duckdb") { std::cerr << "error: --format must be duckdb or parquet\n"; return 2; }
        argi += 2;
    }
    if (argc - argi < 2) {
        std::cerr << "usage: clean [--format duckdb|parquet] <raw_in.csv> "
                     "<events_out.csv|.duckdb|out_dir> [machine_id]\n";
        return 2;
    }
    const std::string in = argv[argi], out = argv[argi + 1];
    const std::string machine = (argc > argi + 2) ? argv[argi + 2] : "MCC";
```

Then, before the existing `.duckdb` branch:

```cpp
    if (parquet) {
        mas::CsvRawReader probe(in);
        if (!probe.is_open()) return report_missing_input(in);
        try {
            mas::ParquetEventStore store(mas::parquet_path_for(out, in), machine);
            const long long n = mas::clean_file(in, store);
            store.close();
            std::cerr << "wrote " << n << " cap events to parquet\n";
        } catch (const std::exception& e) {
            std::cerr << "error: " << e.what() << "\n";
            return 1;
        }
        return 0;
    }
```

Add `#include "mas/store/ParquetEventStore.hpp"` to the includes.

- [ ] **Step 3: `monolith_main.cpp` — one store per input file**

Parse the same flag at the top of `main` (identical block to Step 2, adjusting the
usage string). Then in the `threads == 1` branch, replace the single-store loop
with a per-file store when `parquet` is set:

```cpp
            if (parquet) {
                // A store per input file, not per run: IEventStore::write()
                // never learns that a file is finished, so a shared store
                // would have to buffer all 21.9M events before it could name
                // anything (spec §3.1).
                for (const auto& f : files) {
                    mas::ParquetEventStore store(mas::parquet_path_for(out, f), machine);
                    const long long n = mas::clean_file(f, store);
                    if (n < 0) { std::cerr << "error: cannot clean " << f << "\n"; return 1; }
                    store.close();
                    events += n;
                }
                clean_s = seconds_since(t0);
                rows = events;
            } else if (no_store) {
```

In the MT branch, replace the per-thread `DuckDbEventStore local(...)` with a
per-file `ParquetEventStore` inside the pull loop when `parquet` is set:

```cpp
            auto pull = [&](int t) {
                try {
                    if (parquet) {
                        for (std::size_t i; (i = next.fetch_add(1)) < files.size();) {
                            mas::ParquetEventStore local(
                                mas::parquet_path_for(out, files[i]), machine);
                            per_file[i] = mas::clean_file(files[i], local);
                            local.close();
                            if (per_file[i] < 0) failed = true;
                        }
                        return;
                    }
                    mas::DuckDbEventStore local(thread_store(out, t), machine);
                    for (std::size_t i; (i = next.fetch_add(1)) < files.size();) {
                        per_file[i] = mas::clean_file(files[i], local);
                        if (per_file[i] < 0) failed = true;
                    }
                } catch (const std::exception& e) {
                    std::cerr << "error: thread " << t << ": " << e.what() << "\n";
                    failed = true;
                }
            };
```

and skip the merge entirely under `parquet` — there is nothing to merge:

```cpp
            if (parquet) {
                merge_s = 0.0;
                for (const auto n : per_file) rows += n;
            } else {
                // existing merge_all block unchanged
            }
```

- [ ] **Step 4: Extract `BeatingStore` so the parquet path can keep its heartbeat**

`CleaningWorker` wraps the injected store in a `BeatingStore` so a worker keeps
beating while `clean_file` runs — the coordinator declares a worker dead after
30 s of silence. A parquet lambda that builds its own store bypasses that
decorator and reintroduces the silence the decorator exists to prevent.

Move the class out of `CleaningWorker.cpp`'s anonymous namespace into
`core/include/mas/store/BeatingStore.hpp`, verbatim apart from the namespace:

```cpp
#pragma once
#include "mas/store/EventStore.hpp"
#include <chrono>
#include <functional>
#include <span>
#include <utility>

namespace mas {

// Beats while a file is being cleaned. Decorating the store rather than
// spawning a thread is deliberate: ZeroMQ sockets are not thread-safe, so
// beating from a second thread on the same PUSH socket would be a data race.
// clean_file() writes every 8,192 events, which on a real day-file is roughly
// every 30 ms.
class BeatingStore : public IEventStore {
public:
    BeatingStore(IEventStore& inner, std::function<void()> beat,
                 std::chrono::milliseconds every)
        : inner_(inner), beat_(std::move(beat)), every_(every),
          last_(std::chrono::steady_clock::now()) {}

    void write(std::span<const CapEvent> events) override {
        inner_.write(events);
        const auto now = std::chrono::steady_clock::now();
        if (now - last_ >= every_) {
            last_ = now;
            beat_();
        }
    }

private:
    IEventStore& inner_;
    std::function<void()> beat_;
    std::chrono::milliseconds every_;
    std::chrono::steady_clock::time_point last_;
};

} // namespace mas
```

In `core/src/agent/CleaningWorker.cpp`, delete the class from the anonymous
namespace and `#include "mas/store/BeatingStore.hpp"` instead.

- [ ] **Step 5: Give `CleanFn` the beat callback**

In `core/include/mas/agent/CleaningWorker.hpp`:

```cpp
    // The beat callback is passed so a clean_fn that supplies its own store --
    // the parquet path does -- can decorate it too. Without it that path is
    // silent for the whole file and the coordinator tombstones a live worker.
    using CleanFn = std::function<long long(const std::string&, IEventStore&,
                                            const std::function<void()>&)>;
```

In `CleaningWorker.cpp`, pass it:

```cpp
        BeatingStore beating(store_, [this] { beat(); }, kBeatEvery);
        const long long events = clean_fn_(item->in_path, beating,
                                           [this] { beat(); });
```

Update every existing `CleanFn` lambda in `tests/test_cleaning_worker.cpp` to
take the third parameter and ignore it, e.g.
`[&](const std::string& p, mas::IEventStore& s, const std::function<void()>&) { ... }`.

- [ ] **Step 6: `worker_main.cpp`**

Parse the flag as in Step 2, then:

```cpp
        mas::CleaningWorker worker(work, results, heartbeats, store, worker_id,
            [&](const std::string& path, mas::IEventStore& s,
                const std::function<void()>& beat) {
                if (!parquet) return mas::clean_file(path, s);
                mas::ParquetEventStore pq(mas::parquet_path_for(out, path), machine);
                // Same decorator the worker applies to its injected store, so
                // the parquet path is not silent for the length of a file.
                mas::BeatingStore beating(pq, beat, mas::CleaningWorker::kBeatEvery);
                const long long n = mas::clean_file(path, beating);
                pq.close();
                return n;
            });
```

- [ ] **Step 7: Prove the heartbeat survives the parquet path**

Add to `tests/test_cleaning_worker.cpp`:

```cpp
TEST(CleaningWorker, CleanFnReceivesABeatCallbackItCanUse) {
    // The parquet path supplies its own store, so it must be handed the beat
    // to decorate it with. If CleanFn stops carrying one, that path goes
    // silent for a whole file and the coordinator tombstones a live worker.
    FakeSource work;
    FakeSink results, hbs;
    NullStore store;
    work.queue.push_back(mas::encode(mas::WorkItem{"d1.csv"}));
    int beats_from_fn = 0;
    mas::CleaningWorker w(work, results, hbs, store, "w1",
        [&](const std::string&, mas::IEventStore&,
            const std::function<void()>& beat) {
            beat();
            ++beats_from_fn;
            return 5LL;
        });
    w.run();
    EXPECT_EQ(beats_from_fn, 1);
    EXPECT_GE(hbs.sent.size(), 3u);   // hello + the fn's beat + post-result
}
```

Match `FakeSource`, `FakeSink` and `NullStore` to whatever the file already
uses — read the top of `tests/test_cleaning_worker.cpp` first and reuse its
existing fixtures rather than adding new ones.

- [ ] **Step 8: Verify by hand on one real day-file**

Run:
```bash
D=telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02
mkdir -p /tmp/pq && rm -f /tmp/pq/*.parquet
./build/clean --format parquet "$D"/*2026-02-01.csv /tmp/pq MCC
.venv/bin/python -c "
import duckdb
print(duckdb.sql(\"SELECT COUNT(*) FROM read_parquet('/tmp/pq/*.parquet')\").fetchone()[0])"
```
Expected: `765711` — the same count `clean` produces into DuckDB, and the same
`oracle.py` reports.

- [ ] **Step 9: Confirm the default path is untouched**

Run: `./build/unit_tests 2>&1 | tail -3`
Expected: `[  PASSED  ] 90 tests.`

- [ ] **Step 10: Commit**

```bash
git add core/src/apps/clean_main.cpp core/src/apps/monolith_main.cpp \
        core/src/apps/worker_main.cpp core/include/mas/store/ParquetEventStore.hpp \
        core/src/store/ParquetEventStore.cpp core/include/mas/store/BeatingStore.hpp \
        core/include/mas/agent/CleaningWorker.hpp core/src/agent/CleaningWorker.cpp \
        tests/test_cleaning_worker.cpp
git commit -m "feat(apps): --format parquet, one store per input file

The store is constructed inside the loop rather than once per run: write()
never learns a file is finished, so a shared store would buffer every event
before it could name anything."
```

---

## Task 4: the analytics tier reads Parquet

The task that decides whether this is a store or just a benchmark.

**Files:**
- Modify: `python/analytics/store.py`
- Modify: `python/tests/conftest.py`
- Create: `python/tests/test_backend_parity.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (Python side is independent)
- Produces: `connect(cfg)` accepting a directory; fixture `tiny_store_parquet`

- [ ] **Step 1: Write the failing parity test**

Create `python/tests/test_backend_parity.py`:

```python
"""The same tools, the same data, two storage backends — they must agree.

This is the test that makes the comparison honest. None of the eight tools know
which backend they are reading, because `connect()` presents both as a table
named `cap_events`. A difference in results is therefore a defect in one
backend, not a difference in how it was queried.
"""
import pytest

from analytics.config import Config
from analytics.tools.anomaly import anomalies
from analytics.tools.idle import idle_periods
from analytics.tools.overview import overview
from analytics.tools.speed import capping_speed
from analytics.tools.success import success_rates
from analytics.tools.torque import torque_stats

TOOLS = [
    ("overview", lambda c: overview(c, period="2026-02")),
    ("success_overall", lambda c: success_rates(c, period="2026-02", by="overall")),
    ("success_head", lambda c: success_rates(c, period="2026-02", by="head")),
    ("torque", lambda c: torque_stats(c, period="2026-02")),
    ("speed", lambda c: capping_speed(c, period="2026-02", bucket="hour")),
    ("idle", lambda c: idle_periods(c, period="2026-02")),
    ("anomalies", lambda c: anomalies(c, period="2026-02")),
]


@pytest.mark.parametrize("name,call", TOOLS, ids=[t[0] for t in TOOLS])
def test_both_backends_agree(name, call, tiny_store, tiny_store_parquet):
    duck = call(Config(store_path=tiny_store, machine_id="MCC"))
    pq = call(Config(store_path=tiny_store_parquet, machine_id="MCC"))
    assert duck.status == pq.status, f"{name}: status differs"
    assert duck.values == pq.values, f"{name}: values differ"


def test_parquet_view_deduplicates_a_redispatched_file(tmp_path, tiny_store_parquet):
    """Two files with identical events must present as one set of events.

    This is what replaces the UNIQUE constraint: the write side cannot prevent
    the duplicate, so the read side removes it.
    """
    import shutil
    from analytics.store import connect
    src = sorted(p for p in __import__("pathlib").Path(tiny_store_parquet).glob("*.parquet"))
    assert src, "fixture produced no parquet files"
    shutil.copy(src[0], src[0].with_name("redispatched.parquet"))
    con = connect(Config(store_path=tiny_store_parquet, machine_id="MCC"))
    n = con.execute("SELECT COUNT(*) FROM cap_events").fetchone()[0]
    assert n == 8, f"expected the 8 fixture events once each, got {n}"


def test_empty_parquet_directory_fails_loudly(tmp_path):
    from analytics.store import connect
    empty = tmp_path / "empty_store"
    empty.mkdir()
    with pytest.raises(ValueError, match="no .parquet files"):
        connect(Config(store_path=str(empty), machine_id="MCC"))
```

- [ ] **Step 2: Add the fixture**

Append to `python/tests/conftest.py`:

```python
@pytest.fixture
def tiny_store_parquet(tmp_path, tiny_store):
    """The tiny store's rows, written out as Parquet.

    Built from the DuckDB fixture rather than duplicated, so the two backends
    are provably reading the same events and a parity failure means a backend
    defect rather than a fixture that drifted.
    """
    out = tmp_path / "tiny_parquet"
    out.mkdir()
    con = duckdb.connect(tiny_store, read_only=True)
    con.execute(
        f"COPY (SELECT * FROM cap_events) TO '{out / 'part-0.parquet'}' (FORMAT PARQUET)")
    con.close()
    return str(out)
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd python && ../.venv/bin/python -m pytest tests/test_backend_parity.py -q 2>&1 | tail -5`
Expected: FAIL — `connect()` passes the directory to `duckdb.connect`, which
errors, so every parametrised case errors.

- [ ] **Step 4: Teach `connect()` about directories**

In `python/analytics/store.py`, replace `connect`:

```python
import os

import duckdb


def connect(cfg):
    """A read-only connection whose `cap_events` is the configured store.

    A directory means a Parquet store: `cap_events` becomes a view over its
    files, deduplicated on the event identity. The eight tools cannot tell the
    difference — none of their `FROM cap_events` change, which is what makes a
    comparison between the two backends a comparison of storage rather than of
    two different queries.
    """
    if not os.path.isdir(cfg.store_path):
        return duckdb.connect(cfg.store_path, read_only=True)

    glob = os.path.join(cfg.store_path, "*.parquet")
    import glob as _glob
    if not _glob.glob(glob):
        raise ValueError(
            f"{cfg.store_path} is a directory with no .parquet files in it; "
            "build one with `clean --format parquet` or point store_path at a "
            ".duckdb file")
    con = duckdb.connect(":memory:")
    # DISTINCT ON is what replaces the UNIQUE constraint the DuckDB backend
    # enforces at write time. On disjoint files it removes nothing and still
    # costs a hash aggregation on every query — the price of moving the dedup
    # to the read side, and the reason bench/read_bench.py exists.
    con.execute(
        "CREATE VIEW cap_events AS "
        "SELECT DISTINCT ON (machine_id, head_id, ts) * "
        f"FROM read_parquet('{glob}')")
    return con
```

- [ ] **Step 5: Run the parity tests**

Run: `cd python && ../.venv/bin/python -m pytest tests/test_backend_parity.py -q 2>&1 | tail -3`
Expected: PASS, 9 tests (7 parametrised + 2).

- [ ] **Step 6: Confirm no tool changed and nothing regressed**

Run:
```bash
git diff --name-only python/analytics/tools/ | wc -l   # must print 0
cd python && ../.venv/bin/python -m pytest -q 2>&1 | tail -2
```
Expected: `0`, then `234 passed, 5 skipped` (225 + 9).

- [ ] **Step 7: Commit**

```bash
git add python/analytics/store.py python/tests/conftest.py python/tests/test_backend_parity.py
git commit -m "feat(analytics): read a Parquet store through the same eight tools

connect() presents a directory of Parquet as a view named cap_events, so none
of the 15 FROM cap_events change. That is the property that makes the backend
comparison a comparison of storage rather than of two different queries.

The DISTINCT ON replaces the UNIQUE constraint and moves the dedup to read
time, where it costs a hash aggregation on every query. Measured separately."
```

---

## Task 5: measure both sides

**Files:**
- Modify: `bench/run_bench.sh`
- Create: `bench/read_bench.py`

**Interfaces:**
- Consumes: `mas_monolith --format parquet` (Task 3), `connect()` (Task 4)
- Produces: a `parquet` arch in `bench/results.csv`; `bench/read_results.csv`

- [ ] **Step 1: Add the write-side arch**

In `bench/run_bench.sh`, after the monolith block and inside the `if [ "$ONLY" != mas ]`
guard, add:

```bash
# --- parquet runs -------------------------------------------------------------
# Same clean path, different persistence: no index, no WAL, no merge. The
# comparison is against mono-1T's e2e, where the DuckDB store is 79.8% of
# wall-clock.
for v in "${VOLUMES[@]}"; do
    for rep in 1 2 3; do
        R="$T/run" && rm -rf "$R" && mkdir -p "$R/pq"
        /usr/bin/time -l "$BUILD/mas_monolith" --format parquet "$R/pq" "$MACHINE" 1 \
            "${FILES[@]:0:$v}" 2>"$R/log" || { cat "$R/log"; exit 1; }
        line=$(grep '^monolith:' "$R/log")
        ev=$(echo "$line"    | sed -n 's/.* files, \([0-9]*\) events.*/\1/p')
        clean=$(echo "$line" | sed -n 's/.*clean \([0-9.]*\) s.*/\1/p')
        total=$(echo "$line" | sed -n 's/.*total \([0-9.]*\) s.*/\1/p')
        rows=$(python3 -c "
import duckdb,sys
print(duckdb.sql(\"SELECT COUNT(DISTINCT (machine_id, head_id, ts)) FROM read_parquet('$R/pq/*.parquet')\").fetchone()[0])")
        check_count "$rows" "$v" "parquet v=$v rep=$rep"
        read -r real user sys rss < <(parse_time "$R/log")
        emit_row "parquet" 0 1 "$v" "$rep" "$clean" "0" "$total" "$ev" "$rss" "$user" "$sys" "$real"
        echo "done: parquet v=${v}d rep=$rep total=${total}s"
    done
done
```

- [ ] **Step 2: Write the read-side harness**

Create `bench/read_bench.py`:

```python
#!/usr/bin/env python3
"""Time the three canned reports against each storage backend.

The write side is only half the question. The Parquet backend moves dedup to
read time, so a write saving can be given back on every query. This measures
that, and is allowed to conclude that DuckDB's write-time index is the cheaper
place to pay.

usage: read_bench.py <duckdb-store> <parquet-dir> [repeats]
"""
import csv
import subprocess
import sys
import time

REPORTS = ("kpi", "drift", "anomalies")
OUT = "bench/read_results.csv"


def run_report(store, kind, out_dir):
    t0 = time.perf_counter()
    p = subprocess.run(
        [sys.executable, "-m", "analytics.cli", "report", kind,
         "--config", "-", "--out", out_dir],
        cwd="python", capture_output=True, text=True,
        input=f'{{"store_path": "{store}", "machine_id": "MCC"}}')
    if p.returncode != 0:
        sys.exit(f"{kind} on {store} failed:\n{p.stdout}{p.stderr}")
    return time.perf_counter() - t0


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    duck, pq = sys.argv[1], sys.argv[2]
    reps = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    rows = []
    for rep in range(1, reps + 1):
        for backend, store in (("duckdb", duck), ("parquet", pq)):
            for kind in REPORTS:
                s = run_report(store, kind, f"/tmp/readbench-{backend}")
                rows.append({"backend": backend, "report": kind,
                             "repeat": rep, "seconds": round(s, 3)})
                print(f"{backend:<8}{kind:<11}rep {rep}: {s:.3f}s")
    with open(OUT, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["backend", "report", "repeat", "seconds"])
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Check `analytics.cli` accepts a config on stdin**

Run: `cd python && grep -n '"-"' analytics/cli.py`
Expected: a match. If there is none, `--config -` is unsupported; write the JSON
to a temp file in `run_report` instead and pass its path.

- [ ] **Step 4: Smoke-test both harnesses at the 1-day volume**

Run:
```bash
bench/run_bench.sh --volumes 1 --only mono --out /tmp/pqbench.csv
grep '^parquet' /tmp/pqbench.csv
```
Expected: three rows, and the sweep did not abort — meaning the row counts
matched `oracle_union.py`.

- [ ] **Step 5: Commit**

```bash
git add bench/run_bench.sh bench/read_bench.py
git commit -m "bench: measure the Parquet backend on write and on read

The write side is half the question. Parquet moves dedup to read time, so a
write saving can be handed back on every query; read_bench.py is what stops
the comparison from being decided by the half that flatters it."
```

---

## Task 6: run it and write down what it says

**Files:**
- Modify: `docs/bench/results.md`, `docs/validation-log.md`

- [ ] **Step 1: Build the store both ways at month scale**

Run:
```bash
D=telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02
mkdir -p /tmp/pq-month && rm -f /tmp/pq-month/*.parquet
time ./build/mas_monolith --format parquet /tmp/pq-month MCC 1 "$D"/*.csv
rm -f /tmp/duck-month.duckdb
time ./build/mas_monolith /tmp/duck-month.duckdb MCC 1 "$D"/*.csv
```
Record both wall times. Expect DuckDB near 230 s and Parquet well under it; if
Parquet is not faster on write, stop and report that — it is the result.

- [ ] **Step 2: Verify the two stores agree**

Run:
```bash
.venv/bin/python -c "
import duckdb
d = duckdb.connect('/tmp/duck-month.duckdb', read_only=True).execute(
    'SELECT COUNT(*) FROM cap_events').fetchone()[0]
p = duckdb.sql(\"SELECT COUNT(DISTINCT (machine_id, head_id, ts)) FROM read_parquet('/tmp/pq-month/*.parquet')\").fetchone()[0]
print(f'duckdb {d:,}  parquet {p:,}  {\"MATCH\" if d == p else \"MISMATCH\"}')"
```
Expected: `21,872,663` both, `MATCH`. A mismatch means the backends disagree and
no timing is worth quoting until it is explained.

- [ ] **Step 3: Time the reads**

Run: `.venv/bin/python bench/read_bench.py /tmp/duck-month.duckdb /tmp/pq-month 3`
Expected: `bench/read_results.csv` with 18 rows.

- [ ] **Step 4: Write the finding**

Append a section to `docs/bench/results.md` stating, with the measured numbers:
write time for each backend at month scale; read time per report type for each;
and the net. State plainly which backend wins and under what conditions,
including the case where Parquet writes faster and reads slower for a net loss.
Do not round in a direction that favours a conclusion, and do not quote a ratio
without the two numbers it came from.

- [ ] **Step 5: Append the validation-log entry**

Record: the two commands, both row counts and the fact they matched, the write
and read tables, and the conclusion. If any figure is extrapolated or measured
in a different session from the one it is compared against, say so — the log
already carries that caveat for two earlier sweeps.

- [ ] **Step 6: Full suite**

Run:
```bash
./build/unit_tests 2>&1 | tail -3
cd python && ../.venv/bin/python -m pytest -q 2>&1 | tail -2
```
Expected: `[  PASSED  ] 90 tests.` and `234 passed, 5 skipped`.

- [ ] **Step 7: Commit**

```bash
git add docs/bench/results.md docs/validation-log.md bench/read_results.csv
git commit -m "bench: what the Parquet backend costs on write and on read"
```

---

## Self-review

**Spec coverage.** §3 identity-by-filename → Task 2 (test `ReprocessingOverwritesTheSameFile`).
§3 read-side dedup → Task 4. §3.1 store-per-file → Task 3. §4.1 store → Task 2.
§4.2 flag → Task 3. §4.3 `connect()` → Task 4. §5 error handling → Task 2 (directory
creation, empty file), Task 4 (empty directory). §6 T1–T5 → Task 2; T6 → Task 4.
§7 write and read → Task 5, run in Task 6. §9 success criteria → Tasks 2, 4, 5, 6.

**Resolved before execution.** An earlier draft of Task 3 let the MAS parquet
path lose its in-progress heartbeat, on the grounds that the benchmark uses
`mono`. That was the wrong trade: a path that tombstones live workers after 30 s
is one somebody uses by accident, and "it is documented" has never stopped
anyone. Steps 4-7 extract `BeatingStore` into a header and give `CleanFn` the
beat callback, so the parquet path decorates its own store the same way.
