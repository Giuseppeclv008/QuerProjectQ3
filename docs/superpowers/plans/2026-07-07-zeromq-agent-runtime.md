# ZeroMQ Agent Runtime & Orchestration (Plan 3) Implementation Plan

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

**Goal:** Distribute day-file cleaning across N worker processes with a brokerless ZeroMQ ventilator/worker/sink pipeline (spec §5.2, §8), reusing the existing `clean_file` pipeline and `DuckDbEventStore` unchanged.

**Architecture:** The Coordinator PUSHes `WorkItem` messages (one raw day-file each — spec §8 unit of work) to a load-balanced pool of CleaningWorker processes over ZeroMQ PUSH/PULL; each worker PULLs items, runs the existing `mas::clean_file` into its **own** per-worker DuckDB store (single-writer, spec §14 Q4), and PUSHes a `WorkResult` back to the Coordinator's sink; per-worker stores are merged idempotently at the end via `ATTACH` + `INSERT OR IGNORE`. Domain code (worker loop, coordinator logic, message codec) depends only on `IMessageSource`/`IMessageSink` (spec §7) — only the ZMQ adapters and the executables' `main`s see `zmq.hpp`.

**Tech Stack:** C++20, CMake ≥ 3.16, libzmq v4.3.5 + cppzmq v4.10.0 (FetchContent, hash-pinned), DuckDB v1.2.2 prebuilt, GoogleTest v1.14.0.

## Global Constraints

- **C++20**, CMake ≥ 3.16 (`CMAKE_CXX_STANDARD 20`, `CMAKE_CXX_STANDARD_REQUIRED ON` — already set).
- **FetchContent deps are `URL_HASH SHA256=…` pinned** (repo policy). If CMake reports a hash mismatch, do **not** copy the "actual" hash blindly: download the archive manually with `curl -LO`, inspect it, run `shasum -a 256`, and cross-check before pinning.
- **DIP (spec §7):** domain layer never `#include`s `zmq.hpp` or `duckdb.hpp`. `zmq.hpp` may appear only in `core/include/mas/ZmqTransport.hpp`, `core/src/ZmqTransport.cpp`, `core/src/worker_main.cpp`, `core/src/coordinator_main.cpp`, and `tests/test_zmq_*.cpp`. `duckdb.hpp` stays confined to `core/src/DuckDbEventStore.cpp` and tests (existing pimpl).
- **Single-writer DuckDB (spec §14 Q4):** one `.duckdb` file per worker process; never two writers on one file. Merge happens sink-side after workers finish.
- **Data files stay out of git** (spec §12): CSV/ZIP/duckdb artifacts live in `/tmp` during validation and are deleted afterwards.
- **Naming:** existing namespace `mas`, headers under `core/include/mas/`, sources under `core/src/`, tests under `tests/`. Test binaries follow existing target names (`unit_tests`, `clean_exe`→`clean`). New executables: `mas_worker`, `mas_coordinator`, `mas_merge`.
- **Build/test commands:** configure once with `cmake -S . -B build`; build with `cmake --build build -j`; run tests with `./build/unit_tests` (or `--gtest_filter=…`).
- **Disk:** ~2 GiB free on this machine. libzmq's build adds ~100 MB under `build/_deps`. Do not extract more than the two needed day-files in Task 8, and clean `/tmp/mas_e2e` when done.
- **Out of scope for this plan** (deliberate, later plans): heartbeats + work re-dispatch and REQ/REP registration (spec §5.2/§10 resilience), PUB/SUB fan-out, Docker/compose packaging (spec §12), KPI/anomaly agents, monolith baseline + benchmark harness (spec §9), multi-day partitioned Parquet layout.

**Measured facts this plan relies on** (verified 2026-07-07 against the February 2026 archive):
- Day-file 2026-02-01 yields **765,711** cap events (Plan 2 validation + Python oracle).
- The head `Count` counter is **cumulative across days**: day 01 ends at `118929`, day 02 starts at `118929` and rises to `146677`. Therefore `UNIQUE(machine_id, head_id, cap_seq)` stays collision-free when merging different days of the same machine, and merging per-worker stores of *different* day-files must produce `sum(per-file event counts)` rows (absent a mid-month counter reset — days 01/02 have none).
- CSV column layout: `ts, 36×Count, 36×AppTorque, 36×Status` (oracle indices `python/oracle.py:20-22`).

---

## File Structure

| File | Responsibility |
|---|---|
| `core/include/mas/Message.hpp` + `core/src/Message.cpp` | `Message` frame, `WorkItem`/`WorkResult` structs, encode/decode/STOP codec. Pure, no I/O. |
| `core/include/mas/Transport.hpp` | `IMessageSource`/`IMessageSink` interfaces (spec §7, verbatim shape). Header-only. |
| `core/include/mas/ZmqTransport.hpp` + `core/src/ZmqTransport.cpp` | `ZmqPushSink`, `ZmqPullSource` adapters (the only domain-adjacent code that includes `zmq.hpp`). New CMake lib `mas_transport`. |
| `core/include/mas/CleaningWorker.hpp` + `core/src/CleaningWorker.cpp` | Worker agent loop: recv → clean → send result; stops on STOP/closed source. Clean function injected. Lives in `mas_core`. |
| `core/include/mas/Coordinator.hpp` + `core/src/Coordinator.cpp` | `run_coordinator`: ventilator + sink logic over the interfaces. Lives in `mas_core`. |
| `core/include/mas/DuckDbEventStore.hpp` + `core/src/DuckDbEventStore.cpp` (modify) | add `merge_from(other_db_path)` — idempotent ATTACH-merge. |
| `core/src/worker_main.cpp` → `mas_worker` | Wire ZMQ sockets + DuckDB store + `clean_file` into a worker process. |
| `core/src/coordinator_main.cpp` → `mas_coordinator` | Bind sockets, dispatch day-files, print summary. |
| `core/src/merge_main.cpp` → `mas_merge` | Merge per-worker stores into one. |
| `tests/fakes/FakeTransport.hpp` | In-memory `FakeSource`/`FakeSink` for agent tests. |
| `tests/test_zmq_smoke.cpp`, `tests/test_message.cpp`, `tests/test_zmq_transport.cpp`, `tests/test_cleaning_worker.cpp`, `tests/test_coordinator.cpp` | Per-unit tests. Merge test extends `tests/test_duckdb_event_store.cpp`. |

---

### Task 1: ZeroMQ dependency (libzmq + cppzmq) and smoke test

**Files:**
- Modify: `CMakeLists.txt`
- Test: `tests/test_zmq_smoke.cpp`

**Interfaces:**
- Consumes: nothing new.
- Produces: CMake targets `libzmq` and `cppzmq` linkable by later tasks; proof that a PUSH/PULL roundtrip works in-process.

- [ ] **Step 1: Write the failing (non-compiling) smoke test**

Create `tests/test_zmq_smoke.cpp`:

```cpp
#include <gtest/gtest.h>
#include <string>
#include <zmq.hpp>

// Build/link sanity for libzmq + cppzmq: one PUSH/PULL roundtrip over inproc.
TEST(ZmqSmoke, InprocPushPullRoundtrip) {
    zmq::context_t ctx(0);  // inproc needs no I/O threads
    zmq::socket_t pull(ctx, zmq::socket_type::pull);
    pull.bind("inproc://smoke");            // inproc: bind must precede connect
    zmq::socket_t push(ctx, zmq::socket_type::push);
    push.connect("inproc://smoke");

    push.send(zmq::buffer(std::string("ping")), zmq::send_flags::none);
    zmq::message_t msg;
    const auto n = pull.recv(msg, zmq::recv_flags::none);
    ASSERT_TRUE(n.has_value());
    EXPECT_EQ(msg.to_string(), "ping");
}
```

- [ ] **Step 2: Verify it fails**

Add `tests/test_zmq_smoke.cpp` to the `unit_tests` sources in `CMakeLists.txt` (see Step 3 diff), then run:

```bash
cmake --build build -j 2>&1 | tail -5
```

Expected: FAIL — `'zmq.hpp' file not found` (dependency not declared yet).

- [ ] **Step 3: Declare the dependencies in CMake**

In `CMakeLists.txt`, insert after the googletest `FetchContent_MakeAvailable(googletest)` line (line 12) and before the DuckDB block:

```cmake
# ZeroMQ: libzmq compiled from source + cppzmq header-only bindings (spec §15).
# cppzmq's CMake picks up the in-tree `libzmq` target automatically.
set(ZMQ_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(WITH_PERF_TOOL OFF CACHE BOOL "" FORCE)
set(WITH_DOCS OFF CACHE BOOL "" FORCE)
set(ENABLE_DRAFTS OFF CACHE BOOL "" FORCE)
set(BUILD_STATIC OFF CACHE BOOL "" FORCE)
FetchContent_Declare(libzmq
  URL https://github.com/zeromq/libzmq/releases/download/v4.3.5/zeromq-4.3.5.tar.gz
  URL_HASH SHA256=6653ef5910f17954861fe72332e68b03ca6e4d9c7160eb3a8de5a5a913bfab43)
set(CPPZMQ_BUILD_TESTS OFF CACHE BOOL "" FORCE)
FetchContent_Declare(cppzmq
  URL https://github.com/zeromq/cppzmq/archive/refs/tags/v4.10.0.tar.gz
  URL_HASH SHA256=c81c81bba8a7644c84932225f018b5088743a22999c6d82a2b5f5cd1e6942b74)
FetchContent_MakeAvailable(libzmq cppzmq)
```

Then extend the existing `unit_tests` target: add `tests/test_zmq_smoke.cpp` to its sources and `cppzmq` to `target_link_libraries(unit_tests …)`:

```cmake
add_executable(unit_tests
  tests/test_cap_event_extractor.cpp
  tests/test_csv_raw_reader.cpp
  tests/test_pipeline.cpp
  tests/test_duckdb_smoke.cpp
  tests/test_duckdb_event_store.cpp
  tests/test_zmq_smoke.cpp)
target_link_libraries(unit_tests PRIVATE mas_core GTest::gtest_main duckdb_imported cppzmq)
```

**Hash-mismatch protocol** (Global Constraints): if either `URL_HASH` fails, verify the archive out-of-band before changing the pin — e.g. `curl -LO <same URL> && shasum -a 256 <file>`, compare with the hash published elsewhere (release page, package-manager recipes). A silent hash swap is the supply-chain failure mode this pin exists to catch.

- [ ] **Step 4: Configure, build, run the smoke test**

```bash
cmake -S . -B build && cmake --build build -j
./build/unit_tests --gtest_filter='ZmqSmoke.*'
```

Expected: first configure downloads + compiles libzmq (~1–3 min), then `[  PASSED  ] 1 test.`
Also run the full suite once — `./build/unit_tests` — expected: all tests pass (24 existing + 1 new).

- [ ] **Step 5: Commit**

```bash
git add CMakeLists.txt tests/test_zmq_smoke.cpp
git commit -m "build: add libzmq 4.3.5 + cppzmq 4.10.0 via FetchContent, hash-pinned, with inproc smoke test"
```

---

### Task 2: Message codec, transport interfaces, in-memory fakes

**Files:**
- Create: `core/include/mas/Message.hpp`, `core/src/Message.cpp`, `core/include/mas/Transport.hpp`, `tests/fakes/FakeTransport.hpp`
- Modify: `CMakeLists.txt` (add `Message.cpp` to `mas_core`; add `tests/test_message.cpp` to `unit_tests`; add `tests` include dir)
- Test: `tests/test_message.cpp`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces (used by Tasks 3–7):
  - `struct mas::Message { std::string payload; }`
  - `struct mas::WorkItem { std::string in_path; }`
  - `struct mas::WorkResult { std::string in_path; long long events; double seconds; }`
  - `mas::Message mas::encode(const WorkItem&)` / `mas::encode(const WorkResult&)`
  - `std::optional<mas::WorkItem> mas::decode_work(const Message&)`
  - `std::optional<mas::WorkResult> mas::decode_result(const Message&)`
  - `mas::Message mas::make_stop()` / `bool mas::is_stop(const Message&)`
  - `struct mas::IMessageSource { virtual std::optional<Message> recv() = 0; }` / `struct mas::IMessageSink { virtual void send(const Message&) = 0; }`
  - Test fakes `mas::test::FakeSource` (public `std::deque<Message> queue`) and `mas::test::FakeSink` (public `std::vector<Message> sent`), included as `#include "fakes/FakeTransport.hpp"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_message.cpp`:

```cpp
#include "mas/Message.hpp"
#include "fakes/FakeTransport.hpp"
#include <gtest/gtest.h>

namespace {

TEST(MessageCodec, WorkItemRoundtrips) {
    const auto m = mas::encode(mas::WorkItem{"/data/day-01.csv"});
    const auto w = mas::decode_work(m);
    ASSERT_TRUE(w.has_value());
    EXPECT_EQ(w->in_path, "/data/day-01.csv");
}

TEST(MessageCodec, WorkResultRoundtrips) {
    const auto m = mas::encode(mas::WorkResult{"/data/day-01.csv", 765711, 12.5});
    const auto r = mas::decode_result(m);
    ASSERT_TRUE(r.has_value());
    EXPECT_EQ(r->in_path, "/data/day-01.csv");
    EXPECT_EQ(r->events, 765711);
    EXPECT_DOUBLE_EQ(r->seconds, 12.5);
}

TEST(MessageCodec, StopIsRecognized) {
    EXPECT_TRUE(mas::is_stop(mas::make_stop()));
    EXPECT_FALSE(mas::is_stop(mas::encode(mas::WorkItem{"x"})));
}

TEST(MessageCodec, MalformedPayloadsDecodeToNullopt) {
    EXPECT_FALSE(mas::decode_work(mas::Message{"RESULT\n/x\n1\n0.1"}).has_value()); // wrong tag
    EXPECT_FALSE(mas::decode_work(mas::Message{"WORK"}).has_value());               // missing field
    EXPECT_FALSE(mas::decode_work(mas::Message{"WORK\n"}).has_value());             // empty path
    EXPECT_FALSE(mas::decode_result(mas::Message{"RESULT\n/x\nnotanumber\n0.1"}).has_value());
    EXPECT_FALSE(mas::decode_result(mas::Message{"RESULT\n/x\n5"}).has_value());    // missing field
    EXPECT_FALSE(mas::decode_result(mas::Message{""}).has_value());
}

TEST(FakeTransport, SourceDrainsQueueThenReturnsNullopt) {
    mas::test::FakeSource src;
    src.queue.push_back(mas::Message{"a"});
    src.queue.push_back(mas::Message{"b"});
    const auto m1 = src.recv();
    ASSERT_TRUE(m1.has_value());
    EXPECT_EQ(m1->payload, "a");
    ASSERT_TRUE(src.recv().has_value());
    EXPECT_FALSE(src.recv().has_value());   // drained => closed source
}

TEST(FakeTransport, SinkRecordsSends) {
    mas::test::FakeSink sink;
    sink.send(mas::Message{"x"});
    ASSERT_EQ(sink.sent.size(), 1u);
    EXPECT_EQ(sink.sent[0].payload, "x");
}

} // namespace
```

- [ ] **Step 2: Verify it fails**

Add `tests/test_message.cpp` to `unit_tests` sources and this line after the `target_link_libraries(unit_tests …)` line in `CMakeLists.txt` (fakes are included as `"fakes/FakeTransport.hpp"`):

```cmake
target_include_directories(unit_tests PRIVATE tests)
```

Run: `cmake --build build -j 2>&1 | tail -5`
Expected: FAIL — `'mas/Message.hpp' file not found`.

- [ ] **Step 3: Write the headers and implementation**

Create `core/include/mas/Message.hpp`:

```cpp
#pragma once
#include <optional>
#include <string>

namespace mas {

// One transport frame. Payload is a tag line plus '\n'-separated fields;
// day-file paths therefore must not contain newlines.
struct Message {
    std::string payload;
};

// Ventilator -> worker: process one raw day-file (spec §8 unit of work).
// The worker's store owns machine identity, so the item is just the path.
struct WorkItem {
    std::string in_path;
};

// Worker -> sink: outcome of one WorkItem. events == -1 => input unreadable
// (mirrors clean_file's contract).
struct WorkResult {
    std::string in_path;
    long long events = 0;
    double seconds = 0.0;
};

Message encode(const WorkItem& w);
Message encode(const WorkResult& r);
std::optional<WorkItem> decode_work(const Message& m);
std::optional<WorkResult> decode_result(const Message& m);
Message make_stop();
bool is_stop(const Message& m);

} // namespace mas
```

Create `core/include/mas/Transport.hpp`:

```cpp
#pragma once
#include "mas/Message.hpp"
#include <optional>

namespace mas {

// Transport seam (spec §7; ISP: recv and send split by socket role).
// recv() returning nullopt means "no message": closed/drained source or
// a timed-out poll — either way the caller stops waiting.
struct IMessageSource {
    virtual std::optional<Message> recv() = 0;
    virtual ~IMessageSource() = default;
};

struct IMessageSink {
    virtual void send(const Message& m) = 0;
    virtual ~IMessageSink() = default;
};

} // namespace mas
```

Create `core/src/Message.cpp`:

```cpp
#include "mas/Message.hpp"
#include <exception>
#include <vector>

namespace mas {
namespace {

constexpr const char* kWorkTag = "WORK";
constexpr const char* kResultTag = "RESULT";
constexpr const char* kStopTag = "STOP";

std::vector<std::string> split_lines(const std::string& s) {
    std::vector<std::string> out;
    std::size_t start = 0;
    while (true) {
        const auto nl = s.find('\n', start);
        if (nl == std::string::npos) {
            out.push_back(s.substr(start));
            return out;
        }
        out.push_back(s.substr(start, nl - start));
        start = nl + 1;
    }
}

} // namespace

Message encode(const WorkItem& w) {
    return {std::string(kWorkTag) + "\n" + w.in_path};
}

Message encode(const WorkResult& r) {
    return {std::string(kResultTag) + "\n" + r.in_path + "\n" +
            std::to_string(r.events) + "\n" + std::to_string(r.seconds)};
}

std::optional<WorkItem> decode_work(const Message& m) {
    const auto f = split_lines(m.payload);
    if (f.size() != 2 || f[0] != kWorkTag || f[1].empty()) return std::nullopt;
    return WorkItem{f[1]};
}

std::optional<WorkResult> decode_result(const Message& m) {
    const auto f = split_lines(m.payload);
    if (f.size() != 4 || f[0] != kResultTag) return std::nullopt;
    try {
        return WorkResult{f[1], std::stoll(f[2]), std::stod(f[3])};
    } catch (const std::exception&) {
        return std::nullopt;   // non-numeric events/seconds
    }
}

Message make_stop() { return {kStopTag}; }

bool is_stop(const Message& m) { return m.payload == kStopTag; }

} // namespace mas
```

Create `tests/fakes/FakeTransport.hpp`:

```cpp
#pragma once
#include "mas/Transport.hpp"
#include <deque>
#include <utility>
#include <vector>

namespace mas::test {

// Scripted source: hands out queued messages in order, then reports a
// closed source (nullopt) forever.
struct FakeSource : IMessageSource {
    std::deque<Message> queue;
    std::optional<Message> recv() override {
        if (queue.empty()) return std::nullopt;
        Message m = std::move(queue.front());
        queue.pop_front();
        return m;
    }
};

struct FakeSink : IMessageSink {
    std::vector<Message> sent;
    void send(const Message& m) override { sent.push_back(m); }
};

} // namespace mas::test
```

Add `core/src/Message.cpp` to the `mas_core` sources in `CMakeLists.txt`.

- [ ] **Step 4: Build and run the tests**

```bash
cmake --build build -j && ./build/unit_tests --gtest_filter='MessageCodec.*:FakeTransport.*'
```

Expected: `[  PASSED  ] 6 tests.` Then `./build/unit_tests` — all pass.

- [ ] **Step 5: Commit**

```bash
git add core/include/mas/Message.hpp core/include/mas/Transport.hpp core/src/Message.cpp tests/fakes/FakeTransport.hpp tests/test_message.cpp CMakeLists.txt
git commit -m "feat(transport): Message codec, IMessageSource/IMessageSink seam, test fakes"
```

---

### Task 3: ZeroMQ transport adapters

**Files:**
- Create: `core/include/mas/ZmqTransport.hpp`, `core/src/ZmqTransport.cpp`
- Modify: `CMakeLists.txt` (new `mas_transport` library; link it into `unit_tests`)
- Test: `tests/test_zmq_transport.cpp`

**Interfaces:**
- Consumes: `mas::Message`, `mas::IMessageSource`, `mas::IMessageSink` (Task 2).
- Produces (used by Task 7):
  - `mas::ZmqPushSink(zmq::context_t& ctx, const std::string& endpoint, bool bind)` — `IMessageSink`.
  - `mas::ZmqPullSource(zmq::context_t& ctx, const std::string& endpoint, bool bind, int timeout_ms = -1)` — `IMessageSource`; `recv()` returns `std::nullopt` on timeout (`timeout_ms < 0` blocks forever).
  - CMake target `mas_transport` (public deps: `cppzmq`, include dir `core/include`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_zmq_transport.cpp`:

```cpp
#include "mas/ZmqTransport.hpp"
#include <gtest/gtest.h>

namespace {

TEST(ZmqTransport, PushPullRoundtripOverInproc) {
    zmq::context_t ctx(0);
    // inproc requires bind before connect: construct the bound PULL first.
    mas::ZmqPullSource source(ctx, "inproc://t1", /*bind=*/true, /*timeout_ms=*/1000);
    mas::ZmqPushSink sink(ctx, "inproc://t1", /*bind=*/false);

    sink.send(mas::Message{"hello"});
    const auto m = source.recv();
    ASSERT_TRUE(m.has_value());
    EXPECT_EQ(m->payload, "hello");
}

TEST(ZmqTransport, RecvTimesOutToNullopt) {
    zmq::context_t ctx(0);
    mas::ZmqPullSource source(ctx, "inproc://t2", /*bind=*/true, /*timeout_ms=*/50);
    EXPECT_FALSE(source.recv().has_value());
}

} // namespace
```

- [ ] **Step 2: Verify it fails**

Add `tests/test_zmq_transport.cpp` to `unit_tests` sources. Run `cmake --build build -j 2>&1 | tail -5`.
Expected: FAIL — `'mas/ZmqTransport.hpp' file not found`.

- [ ] **Step 3: Implement the adapters**

Create `core/include/mas/ZmqTransport.hpp`:

```cpp
#pragma once
#include "mas/Transport.hpp"
#include <string>
#include <zmq.hpp>

namespace mas {

// ZeroMQ adapters. DIP boundary (spec §7): only this header, its .cpp and
// the agent mains may include zmq.hpp; domain code sees just the interfaces.

class ZmqPushSink : public IMessageSink {
public:
    ZmqPushSink(zmq::context_t& ctx, const std::string& endpoint, bool bind);
    void send(const Message& m) override;

private:
    zmq::socket_t sock_;
};

class ZmqPullSource : public IMessageSource {
public:
    // timeout_ms < 0 blocks forever; otherwise recv() yields nullopt after
    // timeout_ms of silence.
    ZmqPullSource(zmq::context_t& ctx, const std::string& endpoint, bool bind,
                  int timeout_ms = -1);
    std::optional<Message> recv() override;

private:
    zmq::socket_t sock_;
};

} // namespace mas
```

Create `core/src/ZmqTransport.cpp`:

```cpp
#include "mas/ZmqTransport.hpp"

namespace mas {

ZmqPushSink::ZmqPushSink(zmq::context_t& ctx, const std::string& endpoint, bool bind)
    : sock_(ctx, zmq::socket_type::push) {
    if (bind) sock_.bind(endpoint); else sock_.connect(endpoint);
}

void ZmqPushSink::send(const Message& m) {
    sock_.send(zmq::buffer(m.payload), zmq::send_flags::none);
}

ZmqPullSource::ZmqPullSource(zmq::context_t& ctx, const std::string& endpoint,
                             bool bind, int timeout_ms)
    : sock_(ctx, zmq::socket_type::pull) {
    sock_.set(zmq::sockopt::rcvtimeo, timeout_ms);
    if (bind) sock_.bind(endpoint); else sock_.connect(endpoint);
}

std::optional<Message> ZmqPullSource::recv() {
    zmq::message_t msg;
    const auto n = sock_.recv(msg, zmq::recv_flags::none);
    if (!n.has_value()) return std::nullopt;   // rcvtimeo expired
    return Message{msg.to_string()};
}

} // namespace mas
```

In `CMakeLists.txt`, after the `mas_core` block add:

```cmake
add_library(mas_transport core/src/ZmqTransport.cpp)
target_include_directories(mas_transport PUBLIC core/include)
target_link_libraries(mas_transport PUBLIC cppzmq)
```

and change the `unit_tests` link line to include it:

```cmake
target_link_libraries(unit_tests PRIVATE mas_core mas_transport GTest::gtest_main duckdb_imported cppzmq)
```

- [ ] **Step 4: Build and run**

```bash
cmake --build build -j && ./build/unit_tests --gtest_filter='ZmqTransport.*'
```

Expected: `[  PASSED  ] 2 tests.` Then `./build/unit_tests` — all pass.

- [ ] **Step 5: Commit**

```bash
git add core/include/mas/ZmqTransport.hpp core/src/ZmqTransport.cpp tests/test_zmq_transport.cpp CMakeLists.txt
git commit -m "feat(transport): ZeroMQ PUSH/PULL adapters behind IMessageSource/IMessageSink"
```

---

### Task 4: CleaningWorker agent

**Files:**
- Create: `core/include/mas/CleaningWorker.hpp`, `core/src/CleaningWorker.cpp`
- Modify: `CMakeLists.txt` (add source to `mas_core`; add test to `unit_tests`)
- Test: `tests/test_cleaning_worker.cpp`

**Interfaces:**
- Consumes: `IMessageSource`/`IMessageSink`, codec (Task 2), `mas::IEventStore` (existing, `core/include/mas/EventStore.hpp`).
- Produces (used by Task 7):
  - `mas::CleaningWorker(IMessageSource& work, IMessageSink& results, IEventStore& store, CleanFn clean_fn)` where `using CleanFn = std::function<long long(const std::string&, IEventStore&)>;`
  - `int CleaningWorker::run()` — blocks until STOP or closed source; returns number of work items handled (including failed ones).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cleaning_worker.cpp`:

```cpp
#include "mas/CleaningWorker.hpp"
#include "mas/Message.hpp"
#include "fakes/FakeTransport.hpp"
#include <gtest/gtest.h>
#include <string>
#include <vector>

namespace {

// In-memory store: records writes, never touches disk.
struct FakeStore : mas::IEventStore {
    std::vector<mas::CapEvent> events;
    void write(std::span<const mas::CapEvent> batch) override {
        events.insert(events.end(), batch.begin(), batch.end());
    }
};

TEST(CleaningWorker, ProcessesItemsInOrderThenStopsOnStop) {
    mas::test::FakeSource work;
    work.queue.push_back(mas::encode(mas::WorkItem{"day1.csv"}));
    work.queue.push_back(mas::encode(mas::WorkItem{"day2.csv"}));
    work.queue.push_back(mas::make_stop());
    work.queue.push_back(mas::encode(mas::WorkItem{"day3.csv"}));  // after STOP: never seen
    mas::test::FakeSink results;
    FakeStore store;
    std::vector<std::string> cleaned;
    mas::CleaningWorker w(work, results, store,
        [&](const std::string& path, mas::IEventStore&) -> long long {
            cleaned.push_back(path);
            return path == "day1.csv" ? 10 : 20;
        });

    EXPECT_EQ(w.run(), 2);
    EXPECT_EQ(cleaned, (std::vector<std::string>{"day1.csv", "day2.csv"}));
    ASSERT_EQ(results.sent.size(), 2u);
    const auto r0 = mas::decode_result(results.sent[0]);
    ASSERT_TRUE(r0.has_value());
    EXPECT_EQ(r0->in_path, "day1.csv");
    EXPECT_EQ(r0->events, 10);
    EXPECT_GE(r0->seconds, 0.0);
    const auto r1 = mas::decode_result(results.sent[1]);
    ASSERT_TRUE(r1.has_value());
    EXPECT_EQ(r1->events, 20);
}

TEST(CleaningWorker, ClosedSourceEndsRun) {
    mas::test::FakeSource work;   // empty queue -> nullopt immediately
    mas::test::FakeSink results;
    FakeStore store;
    mas::CleaningWorker w(work, results, store,
        [](const std::string&, mas::IEventStore&) -> long long { return 0; });
    EXPECT_EQ(w.run(), 0);
    EXPECT_TRUE(results.sent.empty());
}

TEST(CleaningWorker, MalformedWorkItemIsSkippedWithoutResult) {
    mas::test::FakeSource work;
    work.queue.push_back(mas::Message{"garbage"});
    work.queue.push_back(mas::encode(mas::WorkItem{"day1.csv"}));
    work.queue.push_back(mas::make_stop());
    mas::test::FakeSink results;
    FakeStore store;
    mas::CleaningWorker w(work, results, store,
        [](const std::string&, mas::IEventStore&) -> long long { return 7; });
    EXPECT_EQ(w.run(), 1);
    ASSERT_EQ(results.sent.size(), 1u);
    EXPECT_EQ(mas::decode_result(results.sent[0])->events, 7);
}

TEST(CleaningWorker, UnreadableInputForwardsMinusOne) {
    mas::test::FakeSource work;
    work.queue.push_back(mas::encode(mas::WorkItem{"missing.csv"}));
    work.queue.push_back(mas::make_stop());
    mas::test::FakeSink results;
    FakeStore store;
    mas::CleaningWorker w(work, results, store,
        [](const std::string&, mas::IEventStore&) -> long long { return -1; });
    EXPECT_EQ(w.run(), 1);
    ASSERT_EQ(results.sent.size(), 1u);
    EXPECT_EQ(mas::decode_result(results.sent[0])->events, -1);
}

} // namespace
```

- [ ] **Step 2: Verify it fails**

Add `tests/test_cleaning_worker.cpp` to `unit_tests` sources; `cmake --build build -j 2>&1 | tail -5`.
Expected: FAIL — `'mas/CleaningWorker.hpp' file not found`.

- [ ] **Step 3: Implement the worker**

Create `core/include/mas/CleaningWorker.hpp`:

```cpp
#pragma once
#include "mas/EventStore.hpp"
#include "mas/Transport.hpp"
#include <functional>
#include <string>

namespace mas {

// Cleaning agent loop (spec §5.3): PULL work items, clean each day-file into
// the injected store, PUSH one WorkResult per item. Exits on STOP or when the
// source reports no more messages. The clean function is injected so tests
// never touch the filesystem; production wires mas::clean_file.
class CleaningWorker {
public:
    using CleanFn = std::function<long long(const std::string&, IEventStore&)>;

    CleaningWorker(IMessageSource& work, IMessageSink& results,
                   IEventStore& store, CleanFn clean_fn);

    // Blocks; returns the number of work items handled (failures included —
    // their WorkResult carries events == -1).
    int run();

private:
    IMessageSource& work_;
    IMessageSink& results_;
    IEventStore& store_;
    CleanFn clean_fn_;
};

} // namespace mas
```

Create `core/src/CleaningWorker.cpp`:

```cpp
#include "mas/CleaningWorker.hpp"
#include "mas/Message.hpp"
#include <chrono>
#include <utility>

namespace mas {

CleaningWorker::CleaningWorker(IMessageSource& work, IMessageSink& results,
                               IEventStore& store, CleanFn clean_fn)
    : work_(work), results_(results), store_(store),
      clean_fn_(std::move(clean_fn)) {}

int CleaningWorker::run() {
    int handled = 0;
    while (auto msg = work_.recv()) {
        if (is_stop(*msg)) break;
        const auto item = decode_work(*msg);
        if (!item) continue;   // malformed frame: drop it, keep serving
        const auto t0 = std::chrono::steady_clock::now();
        const long long events = clean_fn_(item->in_path, store_);
        const std::chrono::duration<double> dt =
            std::chrono::steady_clock::now() - t0;
        results_.send(encode(WorkResult{item->in_path, events, dt.count()}));
        ++handled;
    }
    return handled;
}

} // namespace mas
```

Add `core/src/CleaningWorker.cpp` to `mas_core` sources.

- [ ] **Step 4: Build and run**

```bash
cmake --build build -j && ./build/unit_tests --gtest_filter='CleaningWorker.*'
```

Expected: `[  PASSED  ] 4 tests.` Then full `./build/unit_tests` — all pass.

- [ ] **Step 5: Commit**

```bash
git add core/include/mas/CleaningWorker.hpp core/src/CleaningWorker.cpp tests/test_cleaning_worker.cpp CMakeLists.txt
git commit -m "feat(agent): CleaningWorker loop — PULL work, clean via injected fn, PUSH results"
```

---

### Task 5: Coordinator (ventilator + sink)

**Files:**
- Create: `core/include/mas/Coordinator.hpp`, `core/src/Coordinator.cpp`
- Modify: `CMakeLists.txt` (add source to `mas_core`; add test to `unit_tests`)
- Test: `tests/test_coordinator.cpp`

**Interfaces:**
- Consumes: codec + interfaces (Task 2).
- Produces (used by Task 7):
  - `struct mas::DispatchSummary { long long total_events = 0; int files_ok = 0; int files_failed = 0; }`
  - `mas::DispatchSummary mas::run_coordinator(const std::vector<WorkItem>& items, IMessageSink& work, IMessageSource& results, int num_workers)`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_coordinator.cpp`:

```cpp
#include "mas/Coordinator.hpp"
#include "fakes/FakeTransport.hpp"
#include <gtest/gtest.h>
#include <vector>

namespace {

TEST(Coordinator, DispatchesAllItemsCollectsResultsThenStopsWorkers) {
    const std::vector<mas::WorkItem> items = {{"d1.csv"}, {"d2.csv"}, {"d3.csv"}};
    mas::test::FakeSink work;
    mas::test::FakeSource results;   // out-of-order results are fine
    results.queue.push_back(mas::encode(mas::WorkResult{"d1.csv", 10, 0.1}));
    results.queue.push_back(mas::encode(mas::WorkResult{"d3.csv", 30, 0.3}));
    results.queue.push_back(mas::encode(mas::WorkResult{"d2.csv", 20, 0.2}));

    const auto s = mas::run_coordinator(items, work, results, /*num_workers=*/2);

    EXPECT_EQ(s.files_ok, 3);
    EXPECT_EQ(s.files_failed, 0);
    EXPECT_EQ(s.total_events, 60);
    ASSERT_EQ(work.sent.size(), 5u);   // 3 WORK then 2 STOP
    for (int i = 0; i < 3; ++i)
        EXPECT_TRUE(mas::decode_work(work.sent[i]).has_value()) << i;
    EXPECT_TRUE(mas::is_stop(work.sent[3]));
    EXPECT_TRUE(mas::is_stop(work.sent[4]));
}

TEST(Coordinator, CountsFailedFilesAndUnreportedItems) {
    const std::vector<mas::WorkItem> items = {{"d1.csv"}, {"d2.csv"}, {"d3.csv"}};
    mas::test::FakeSink work;
    mas::test::FakeSource results;
    results.queue.push_back(mas::encode(mas::WorkResult{"d1.csv", -1, 0.0}));  // unreadable
    results.queue.push_back(mas::encode(mas::WorkResult{"d2.csv", 20, 0.2}));
    // d3 never reports (dead worker) -> source dries up (nullopt)

    const auto s = mas::run_coordinator(items, work, results, /*num_workers=*/1);

    EXPECT_EQ(s.files_ok, 1);
    EXPECT_EQ(s.files_failed, 2);      // one events==-1, one never reported
    EXPECT_EQ(s.total_events, 20);
    ASSERT_EQ(work.sent.size(), 4u);   // 3 WORK + 1 STOP: STOPs still sent
    EXPECT_TRUE(mas::is_stop(work.sent.back()));
}

TEST(Coordinator, NoItemsStillStopsWorkers) {
    mas::test::FakeSink work;
    mas::test::FakeSource results;
    const auto s = mas::run_coordinator({}, work, results, /*num_workers=*/3);
    EXPECT_EQ(s.files_ok, 0);
    EXPECT_EQ(s.files_failed, 0);
    ASSERT_EQ(work.sent.size(), 3u);
    for (const auto& m : work.sent) EXPECT_TRUE(mas::is_stop(m));
}

} // namespace
```

- [ ] **Step 2: Verify it fails**

Add `tests/test_coordinator.cpp` to `unit_tests` sources; `cmake --build build -j 2>&1 | tail -5`.
Expected: FAIL — `'mas/Coordinator.hpp' file not found`.

- [ ] **Step 3: Implement the coordinator**

Create `core/include/mas/Coordinator.hpp`:

```cpp
#pragma once
#include "mas/Message.hpp"
#include "mas/Transport.hpp"
#include <vector>

namespace mas {

struct DispatchSummary {
    long long total_events = 0;
    int files_ok = 0;      // results with events >= 0
    int files_failed = 0;  // events < 0, malformed results, or never reported
};

// Ventilator + sink in one call (spec §8): PUSH every item, PULL one result
// per item, then PUSH one STOP per worker so the pool shuts down. If the
// result source reports no more messages (timeout/closed) the remaining
// unreported items count as failed — the caller decides what to do; work
// re-dispatch on missed heartbeats is a later plan (spec §10).
DispatchSummary run_coordinator(const std::vector<WorkItem>& items,
                                IMessageSink& work, IMessageSource& results,
                                int num_workers);

} // namespace mas
```

Create `core/src/Coordinator.cpp`:

```cpp
#include "mas/Coordinator.hpp"

namespace mas {

DispatchSummary run_coordinator(const std::vector<WorkItem>& items,
                                IMessageSink& work, IMessageSource& results,
                                int num_workers) {
    for (const auto& item : items) work.send(encode(item));

    DispatchSummary s;
    std::size_t received = 0;
    while (received < items.size()) {
        const auto msg = results.recv();
        if (!msg) break;   // sink went silent: give up on the stragglers
        ++received;
        const auto r = decode_result(*msg);
        if (r && r->events >= 0) {
            ++s.files_ok;
            s.total_events += r->events;
        } else {
            ++s.files_failed;
        }
    }
    s.files_failed += static_cast<int>(items.size() - received);

    for (int i = 0; i < num_workers; ++i) work.send(make_stop());
    return s;
}

} // namespace mas
```

Add `core/src/Coordinator.cpp` to `mas_core` sources.

- [ ] **Step 4: Build and run**

```bash
cmake --build build -j && ./build/unit_tests --gtest_filter='Coordinator.*'
```

Expected: `[  PASSED  ] 3 tests.` Then full `./build/unit_tests` — all pass.

- [ ] **Step 5: Commit**

```bash
git add core/include/mas/Coordinator.hpp core/src/Coordinator.cpp tests/test_coordinator.cpp CMakeLists.txt
git commit -m "feat(agent): coordinator ventilator/sink — dispatch, collect, stop workers"
```

---

### Task 6: Store merge (`merge_from`) + `mas_merge` executable

**Files:**
- Modify: `core/include/mas/DuckDbEventStore.hpp` (add method), `core/src/DuckDbEventStore.cpp` (implement), `CMakeLists.txt` (new `merge_exe` target)
- Create: `core/src/merge_main.cpp`
- Test: `tests/test_duckdb_event_store.cpp` (extend — reuse its `ev()`/`removeDb()` helpers)

**Interfaces:**
- Consumes: existing `DuckDbEventStore(db_path, machine_id)`, `count()`, internal `execOrThrow`.
- Produces (used by Task 8):
  - `void DuckDbEventStore::merge_from(const std::string& other_db_path)` — idempotent union of another store's `cap_events` into this one.
  - Executable `mas_merge <dst.duckdb> <machine_id> <src1.duckdb> [src2.duckdb ...]`; prints `merged K stores; dst holds N rows` to stderr; exit 0 on success, 1 on error, 2 on usage.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_duckdb_event_store.cpp` (inside its anonymous namespace, before the closing brace — the `ev` and `removeDb` helpers at the top of the file are in scope):

```cpp
TEST(DuckDbEventStore, MergeFromUnionsStoresIdempotently) {
    const std::string a = "t_merge_a.duckdb";
    const std::string b = "t_merge_b.duckdb";
    const std::string dst = "t_merge_dst.duckdb";
    removeDb(a); removeDb(b); removeDb(dst);
    {
        mas::DuckDbEventStore sa(a, "MCC1");
        std::vector<mas::CapEvent> ba = {
            ev(1, "2026-02-01T00:00:01.000", 101),
            ev(1, "2026-02-01T00:00:02.000", 102),
        };
        sa.write(ba);
        mas::DuckDbEventStore sb(b, "MCC1");
        std::vector<mas::CapEvent> bb = {
            ev(1, "2026-02-01T00:00:02.000", 102),   // overlaps store a
            ev(2, "2026-02-01T00:00:03.000", 55),
        };
        sb.write(bb);
    }   // close source stores before attaching them read-only

    mas::DuckDbEventStore d(dst, "MCC1");
    d.merge_from(a);
    d.merge_from(b);
    EXPECT_EQ(d.count(), 3);   // union: (1,101), (1,102), (2,55)
    d.merge_from(b);           // re-merge is safe (spec §10 idempotency)
    EXPECT_EQ(d.count(), 3);
    removeDb(a); removeDb(b); removeDb(dst);
}
```

- [ ] **Step 2: Verify it fails**

```bash
cmake --build build -j 2>&1 | tail -5
```

Expected: FAIL — `no member named 'merge_from' in 'mas::DuckDbEventStore'`.

- [ ] **Step 3: Implement `merge_from` and `mas_merge`**

In `core/include/mas/DuckDbEventStore.hpp`, add after the `export_parquet` declaration:

```cpp
    // Idempotently union another store file's cap_events into this one
    // (ATTACH read-only + INSERT OR IGNORE). Sink-side answer to spec §14
    // Q4: workers write per-worker stores, never one file concurrently.
    void merge_from(const std::string& other_db_path);
```

In `core/src/DuckDbEventStore.cpp`, add after `export_parquet`:

```cpp
void DuckDbEventStore::merge_from(const std::string& other_db_path) {
    // Same trusted-local-path caveat as export_parquet: the path is spliced
    // into SQL, so a quote in it would break the statement.
    execOrThrow(impl_->con, "ATTACH '" + other_db_path + "' AS src (READ_ONLY)");
    execOrThrow(impl_->con, R"sql(
        INSERT OR IGNORE INTO cap_events (machine_id, head_id, ts, cap_seq, app_torque, status, delta, is_fault, aggregated, is_reset)
        SELECT machine_id, head_id, ts, cap_seq, app_torque, status, delta, is_fault, aggregated, is_reset
        FROM src.cap_events)sql");
    execOrThrow(impl_->con, "DETACH src");
}
```

Create `core/src/merge_main.cpp`:

```cpp
#include "mas/DuckDbEventStore.hpp"
#include <exception>
#include <iostream>

int main(int argc, char** argv) {
    if (argc < 4) {
        std::cerr << "usage: mas_merge <dst.duckdb> <machine_id> <src1.duckdb> [src2.duckdb ...]\n";
        return 2;
    }
    try {
        mas::DuckDbEventStore dst(argv[1], argv[2]);
        for (int i = 3; i < argc; ++i) dst.merge_from(argv[i]);
        std::cerr << "merged " << (argc - 3) << " stores; dst holds "
                  << dst.count() << " rows\n";
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << "\n";
        return 1;
    }
    return 0;
}
```

In `CMakeLists.txt`, next to the `clean_exe` block add:

```cmake
add_executable(merge_exe core/src/merge_main.cpp)
target_link_libraries(merge_exe PRIVATE mas_core)
set_target_properties(merge_exe PROPERTIES OUTPUT_NAME mas_merge)
```

and add `merge_exe` to the existing `BUILD_RPATH` property line so it finds `libduckdb` at runtime:

```cmake
set_target_properties(unit_tests clean_exe merge_exe PROPERTIES
  BUILD_RPATH "${duckdb_prebuilt_SOURCE_DIR}")
```

- [ ] **Step 4: Build, run test, exercise the CLI**

```bash
cmake --build build -j && ./build/unit_tests --gtest_filter='DuckDbEventStore.MergeFromUnionsStoresIdempotently'
./build/mas_merge 2>&1; echo "exit=$?"
```

Expected: test PASSES; the bare CLI call prints the usage line and `exit=2`. Full `./build/unit_tests` — all pass.

- [ ] **Step 5: Commit**

```bash
git add core/include/mas/DuckDbEventStore.hpp core/src/DuckDbEventStore.cpp core/src/merge_main.cpp tests/test_duckdb_event_store.cpp CMakeLists.txt
git commit -m "feat(store): idempotent merge_from across store files + mas_merge CLI"
```

---

### Task 7: `mas_worker` and `mas_coordinator` executables

**Files:**
- Create: `core/src/worker_main.cpp`, `core/src/coordinator_main.cpp`
- Modify: `CMakeLists.txt` (two new targets)

**Interfaces:**
- Consumes: `ZmqPushSink`/`ZmqPullSource` (Task 3), `CleaningWorker` (Task 4), `run_coordinator` (Task 5), existing `DuckDbEventStore` and `mas::clean_file(const std::string&, IEventStore&)` (`core/include/mas/Pipeline.hpp`).
- Produces (used by Task 8):
  - `mas_worker <work_endpoint> <result_endpoint> <out.duckdb> [machine_id]` — connects PULL to work, PUSH to results; runs until STOP; prints `worker done: K work items, store holds N rows`; exit 0/1/2.
  - `mas_coordinator <work_endpoint> <result_endpoint> <num_workers> <day1.csv> [day2.csv ...]` — binds both sockets; prints `dispatched F files: A ok, B failed, N events`; exit 0 if no failures, 1 otherwise, 2 on usage.

- [ ] **Step 1: Write `worker_main.cpp`**

```cpp
#include "mas/CleaningWorker.hpp"
#include "mas/DuckDbEventStore.hpp"
#include "mas/Pipeline.hpp"
#include "mas/ZmqTransport.hpp"
#include <exception>
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    if (argc < 4) {
        std::cerr << "usage: mas_worker <work_endpoint> <result_endpoint> <out.duckdb> [machine_id]\n";
        return 2;
    }
    const std::string work_ep = argv[1], result_ep = argv[2], out = argv[3];
    const std::string machine = (argc > 4) ? argv[4] : "MCC";
    try {
        zmq::context_t ctx(1);
        mas::ZmqPullSource work(ctx, work_ep, /*bind=*/false);
        mas::ZmqPushSink results(ctx, result_ep, /*bind=*/false);
        mas::DuckDbEventStore store(out, machine);
        mas::CleaningWorker worker(work, results, store,
            [](const std::string& path, mas::IEventStore& s) {
                return mas::clean_file(path, s);
            });
        const int handled = worker.run();
        std::cerr << "worker done: " << handled << " work items, store holds "
                  << store.count() << " rows\n";
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << "\n";
        return 1;
    }
    return 0;
}
```

- [ ] **Step 2: Write `coordinator_main.cpp`**

```cpp
#include "mas/Coordinator.hpp"
#include "mas/ZmqTransport.hpp"
#include <exception>
#include <iostream>
#include <string>
#include <vector>

int main(int argc, char** argv) {
    if (argc < 5) {
        std::cerr << "usage: mas_coordinator <work_endpoint> <result_endpoint> <num_workers> <day1.csv> [day2.csv ...]\n";
        return 2;
    }
    try {
        const std::string work_ep = argv[1], result_ep = argv[2];
        const int num_workers = std::stoi(argv[3]);
        if (num_workers < 1) {
            std::cerr << "error: num_workers must be >= 1\n";
            return 2;
        }
        std::vector<mas::WorkItem> items;
        for (int i = 4; i < argc; ++i) items.push_back({argv[i]});

        zmq::context_t ctx(1);
        mas::ZmqPushSink work(ctx, work_ep, /*bind=*/true);
        // 60 s of sink silence => count stragglers as failed instead of
        // hanging forever (heartbeat-driven re-dispatch is a later plan).
        mas::ZmqPullSource results(ctx, result_ep, /*bind=*/true,
                                   /*timeout_ms=*/60000);
        const auto s = mas::run_coordinator(items, work, results, num_workers);
        std::cerr << "dispatched " << items.size() << " files: " << s.files_ok
                  << " ok, " << s.files_failed << " failed, "
                  << s.total_events << " events\n";
        return s.files_failed == 0 ? 0 : 1;
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << "\n";
        return 1;
    }
}
```

- [ ] **Step 3: Add CMake targets**

In `CMakeLists.txt`, next to the other executables:

```cmake
add_executable(worker_exe core/src/worker_main.cpp)
target_link_libraries(worker_exe PRIVATE mas_core mas_transport)
set_target_properties(worker_exe PROPERTIES OUTPUT_NAME mas_worker)

add_executable(coordinator_exe core/src/coordinator_main.cpp)
target_link_libraries(coordinator_exe PRIVATE mas_core mas_transport)
set_target_properties(coordinator_exe PROPERTIES OUTPUT_NAME mas_coordinator)
```

and extend the `BUILD_RPATH` line once more (workers/coordinator link `mas_core`, which needs `libduckdb` at runtime):

```cmake
set_target_properties(unit_tests clean_exe merge_exe worker_exe coordinator_exe PROPERTIES
  BUILD_RPATH "${duckdb_prebuilt_SOURCE_DIR}")
```

- [ ] **Step 4: Build and smoke-test the wiring end-to-end on synthetic data**

```bash
cmake --build build -j
printf 'ts,%s,%s,%s\n2026-02-01T00:00:00.000,%s\n2026-02-01T00:00:01.000,%s\n' \
  "$(python3 -c "print(','.join(f'H{h:02d} Count' for h in range(1,37)))")" \
  "$(python3 -c "print(','.join(f'H{h:02d} AppTorque' for h in range(1,37)))")" \
  "$(python3 -c "print(','.join(f'H{h:02d} Status' for h in range(1,37)))")" \
  "$(python3 -c "print(','.join(['1.0']*36 + ['2.0']*36 + ['2.0']*36))")" \
  "$(python3 -c "print(','.join(['2.0']*36 + ['2.0']*36 + ['2.0']*36))")" \
  > /tmp/mini_day.csv
./build/mas_worker tcp://127.0.0.1:5557 tcp://127.0.0.1:5558 /tmp/mini_w1.duckdb MCC &
sleep 1
./build/mas_coordinator tcp://127.0.0.1:5557 tcp://127.0.0.1:5558 1 /tmp/mini_day.csv
wait
rm -f /tmp/mini_day.csv /tmp/mini_w1.duckdb /tmp/mini_w1.duckdb.wal
```

Expected: coordinator prints `dispatched 1 files: 1 ok, 0 failed, 36 events` and exits 0 (each of 36 heads increments once); worker prints `worker done: 1 work items, store holds 36 rows`. Also run `./build/unit_tests` — all pass.

- [ ] **Step 5: Commit**

```bash
git add core/src/worker_main.cpp core/src/coordinator_main.cpp CMakeLists.txt
git commit -m "feat(cli): mas_worker and mas_coordinator — ZeroMQ ventilator/worker/sink over real sockets"
```

---

### Task 8: Multi-process real-data validation (2 workers × 2 day-files)

**Files:**
- Modify: `docs/validation-log.md` (append entry)

**Interfaces:**
- Consumes: `mas_worker`, `mas_coordinator` (Task 7), `mas_merge` (Task 6), `python/oracle.py` (existing: `python3 python/oracle.py <csv>` prints the expected event count).
- Produces: committed validation evidence; no code.

- [ ] **Step 1: Stage the two day-files**

```bash
mkdir -p /tmp/mas_e2e
unzip -o telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02.zip \
  telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02-01.csv \
  telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02-02.csv \
  -d /tmp/mas_e2e/
```

- [ ] **Step 2: Compute oracle expectations**

```bash
python3 python/oracle.py /tmp/mas_e2e/telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02-01.csv
python3 python/oracle.py /tmp/mas_e2e/telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02-02.csv
```

Expected: day 01 prints `765711` (known from Plan 2). Record day 02's count — call it `D2`. Expected merged total `TOTAL = 765711 + D2` (collision-free because the Count counter is cumulative across these days: day 01 ends at 118929, day 02 starts there — see Measured facts).

- [ ] **Step 3: Run the distributed pipeline — workers first (PUSH round-robins over already-connected peers), then coordinator**

```bash
./build/mas_worker tcp://127.0.0.1:5557 tcp://127.0.0.1:5558 /tmp/mas_e2e/w1.duckdb MCC &
./build/mas_worker tcp://127.0.0.1:5557 tcp://127.0.0.1:5558 /tmp/mas_e2e/w2.duckdb MCC &
sleep 1
./build/mas_coordinator tcp://127.0.0.1:5557 tcp://127.0.0.1:5558 2 \
  /tmp/mas_e2e/telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02-01.csv \
  /tmp/mas_e2e/telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02-02.csv
wait
```

Expected: coordinator prints `dispatched 2 files: 2 ok, 0 failed, TOTAL events` (exit 0); each worker prints `worker done: 1 work items, …` (with 2 workers pre-connected, round-robin gives one file each; if one worker reports 2 items and the other 0, that is load-skew, not an error — the totals still must match).

- [ ] **Step 4: Merge per-worker stores and verify the union**

```bash
./build/mas_merge /tmp/mas_e2e/merged.duckdb MCC /tmp/mas_e2e/w1.duckdb /tmp/mas_e2e/w2.duckdb
./build/mas_merge /tmp/mas_e2e/merged.duckdb MCC /tmp/mas_e2e/w1.duckdb /tmp/mas_e2e/w2.duckdb
```

Expected: both runs print `merged 2 stores; dst holds TOTAL rows` — the second run proves merge idempotency on real data. If the merged count is **less** than `TOTAL`, cap_seq collided across day-files (a counter reset between the two days) — stop and investigate before committing; days 01/02 were verified reset-free.

- [ ] **Step 5: Append the validation entry and commit**

Append to `docs/validation-log.md` (fill in the measured numbers from Steps 2–4, replacing `D2`, `TOTAL`, and the timing you observed):

```markdown
## 2026-07-XX — Plan 3: ZeroMQ MAS, 2 workers × 2 day-files (real data)

- Files: 2026-02-01 (86,399 rows) and 2026-02-02 day-files.
- Oracle expectations: day 01 = 765,711; day 02 = D2; total = TOTAL.
- `mas_coordinator` (2 workers, tcp://127.0.0.1:5557/5558): "dispatched 2 files: 2 ok, 0 failed, TOTAL events".
- Per-worker stores merged via `mas_merge`: merged store holds TOTAL rows; second merge run unchanged (idempotent, spec §10).
- Counter continuity verified: day 01 ends at Count 118929, day 02 starts at 118929 -> UNIQUE(machine_id, head_id, cap_seq) is collision-free across days (spec §14 Q3: cumulative counter; day-boundary seeding loses no caps between these days).
- Spec cross-refs: §5.2 PUSH/PULL fabric, §8 ventilator/worker/sink + day-file work unit, §14 Q4 per-worker stores merged at sink.
```

Then clean up and commit:

```bash
rm -rf /tmp/mas_e2e
git add docs/validation-log.md
git commit -m "docs: validation log — ZeroMQ 2-worker pipeline on real data, merged store matches oracle"
```

---

## Self-Review

**Spec coverage (Plan 3 scope = spec §5.2 PUSH/PULL fabric + §7 transport/agent seams + §8 ventilator/worker/sink + §14 Q4 store concurrency):**
- `IMessageSource`/`IMessageSink` with spec §7's exact shapes → Task 2. ✓
- ZeroMQ PUSH/PULL adapters, brokerless (spec §5.2) → Tasks 1, 3. ✓
- Cleaning agent as recv→clean→publish loop (spec §5.3 boxes: Work Receiver, Event Writer via injected store, Result Publisher) → Task 4; parsing/dedup reused from Plans 1–2 (`clean_file`). ✓
- Coordinator ventilator + sink, unit of work = day-file (spec §8) → Task 5. ✓
- Per-worker single-writer stores merged at the sink (spec §14 Q4 resolution) → Task 6. ✓
- Multi-process integration on a known day with 2 workers, asserting counts vs the reference (spec §11 "Integration") → Task 8 (docker-compose variant deferred with Docker packaging). ✓
- Backpressure (spec §10): inherent in PUSH/PULL blocking semantics; no code needed this plan. Heartbeats/re-dispatch, registration REQ/REP, PUB/SUB, Docker, benchmarks: explicitly out of scope (Global Constraints) — next plans.
- Spec §14 Q3 (cross-day continuity) resolved by measurement: cumulative counter, recorded in Task 8's log entry.

**Placeholder scan:** every code step carries complete code; every command has expected output. Task 8 uses `D2`/`TOTAL` as *measured-at-execution* values with the exact commands that produce them — not placeholders for missing design. No TBD/TODO. ✓

**Type consistency:** `WorkItem{in_path}` (Task 2) matches usage in Tasks 4, 5, 7 (`items.push_back({argv[i]})`). `CleanFn = std::function<long long(const std::string&, IEventStore&)>` matches `mas::clean_file(const std::string&, IEventStore&)` → wrapped in a lambda in `worker_main` (overload disambiguation). `run_coordinator(items, work, results, num_workers)` identical in Tasks 5 and 7. `ZmqPullSource(ctx, endpoint, bind, timeout_ms)` identical in Tasks 3 and 7. `merge_from(const std::string&)` identical in Task 6 test/impl/CLI. `unit_tests` link list evolves monotonically (Task 1 adds `cppzmq`, Task 3 replaces with `mas_core mas_transport … cppzmq`). ✓
