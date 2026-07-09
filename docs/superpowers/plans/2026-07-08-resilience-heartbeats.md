# Resilience: Heartbeats & Work Re-dispatch — Implementation Plan (Plan 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect dead cleaning workers via heartbeats and re-dispatch their work so a mid-run `kill -9` still produces oracle-exact counts; make every process self-terminating (no hangs).

**Architecture:** A third PUSH/PULL channel (worker→coordinator) carries heartbeats, reusing the existing `ZmqPushSink`/`ZmqPullSource` adapters. `run_coordinator` becomes a tick loop with an injectable clock: it registers workers from heartbeats/results, attributes completions per worker, tombstones workers silent past a threshold, and re-dispatches every open item plus the dead worker's completions (its store file is written off; the idempotent UNIQUE upsert + `merge_from` absorb all resulting duplicates). The worker stays single-threaded: its wait for work is a 1 s-timeout recv loop that heartbeats on every empty tick and self-exits after 60 consecutive empty ticks.

**Tech Stack:** C++17, ZeroMQ (libzmq 4.3.5 / cppzmq 4.10.0, already vendored), DuckDB (vendored), GoogleTest, CMake ≥ 3.24.

**Spec:** `docs/superpowers/specs/2026-07-08-resilience-heartbeats-design.md` — the authority on semantics. Read it before starting.

## Global Constraints

- **DIP boundary (parent spec §7):** only `ZmqTransport.hpp`, `ZmqTransport.cpp`, and the agent mains may include `zmq.hpp`. Domain code (`Coordinator`, `CleaningWorker`, `Message`) sees only `IMessageSource`/`IMessageSink`.
- **Strict codec:** wire bytes are untrusted. Numeric fields parse with full-consumption checks (`std::stoll`/`std::stod` + `pos == size()`); malformed frames decode to `std::nullopt`, never throw out of the decoder.
- **TDD:** every behavior lands RED → GREEN. Run the named test binary and paste real output; a test that passes on arrival must be investigated, not shrugged at.
- **Defaults (spec §9, exact values):** worker wait tick 1000 ms; death threshold 30 000 ms; coordinator results-recv timeout 200 ms; heartbeat-source recv timeout 0 ms (non-blocking drain); re-dispatch cap 2 per item; worker idle exit after 60 consecutive empty ticks; send timeout 60 000 ms on every PUSH socket. Thresholds are constants in the mains — no new CLI knobs.
- **Both ends ship together:** the wire format changes (RESULT gains a field, HB is new). No backward compatibility with Plan 3 frames is required or attempted.
- **No new dependencies.** No new threads.
- **Build & test:** `cmake --build build -j` then `./build/unit_tests`. All pre-existing tests must stay green at every commit (updating them for new signatures is part of the relevant task, not optional).
- **Commits:** conventional-commit style, one per task step-5, ending with the project's standard trailer.
- **Branch:** execute on `feat/resilience-heartbeats` (create from `main` at execution time via the worktrees skill).

## File Structure (locked)

| File | Change | Responsibility |
|---|---|---|
| `core/include/mas/Message.hpp` + `core/src/Message.cpp` | modify | `Heartbeat` struct + codec; `WorkResult.worker_id` |
| `core/include/mas/CleaningWorker.hpp` + `core/src/CleaningWorker.cpp` | modify | heartbeat emission, idle-tick self-exit, `worker_id` stamping |
| `core/include/mas/Coordinator.hpp` + `core/src/Coordinator.cpp` | rewrite | tick loop, registry, attribution, death sweep, re-dispatch, aborts |
| `core/src/worker_main.cpp` | modify | new CLI (`hb_ep`, `worker_id`), timeout wiring |
| `core/src/coordinator_main.cpp` | modify | new CLI (`hb_ep`, drops `num_workers`), clock + config wiring |
| `core/src/merge_main.cpp` | modify | per-source skip on ATTACH failure |
| `tests/fakes/FakeTransport.hpp` | modify | add `FakeTickSource` (scriptable empty ticks) |
| `tests/test_message.cpp` | modify | HB round-trip; RESULT format v2 |
| `tests/test_cleaning_worker.cpp` | modify | heartbeat/idle behaviors; new ctor |
| `tests/test_coordinator.cpp` | rewrite | new signature + registry/re-dispatch behaviors |
| `scripts/chaos_e2e.sh` | create | kill-a-worker end-to-end validation |
| `docs/validation-log.md` | append | chaos-run evidence |

No CMake edits: all test files already belong to `unit_tests`; the script is not a ctest.

---

### Task 1: Codec — `Heartbeat` message + `WorkResult.worker_id`

**Files:**
- Modify: `core/include/mas/Message.hpp`
- Modify: `core/src/Message.cpp`
- Test: `tests/test_message.cpp`

**Interfaces:**
- Consumes: existing tag-line codec conventions in `Message.cpp` (`split_lines`, full-consumption numeric parsing).
- Produces (later tasks rely on these exact shapes):
  - `struct Heartbeat { std::string worker_id; long long seq = 0; };`
  - `Message encode(const Heartbeat& h);` — payload `"HB\n<worker_id>\n<seq>"`
  - `std::optional<Heartbeat> decode_heartbeat(const Message& m);`
  - `struct WorkResult { std::string in_path; long long events = 0; double seconds = 0.0; std::string worker_id; };` — **worker_id is the new 4th member**, wire line 5 of the RESULT payload.
  - `decode_result` now requires exactly 5 lines and a non-empty `worker_id`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_message.cpp` (inside its anonymous namespace, matching the file's existing style):

```cpp
TEST(Message, HeartbeatRoundTrip) {
    const mas::Heartbeat h{"w1", 42};
    const auto m = mas::encode(h);
    const auto d = mas::decode_heartbeat(m);
    ASSERT_TRUE(d.has_value());
    EXPECT_EQ(d->worker_id, "w1");
    EXPECT_EQ(d->seq, 42);
}

TEST(Message, DecodeHeartbeatRejectsMalformed) {
    // wrong tag, wrong arity, empty id, non-numeric / trailing-garbage /
    // negative seq: all rejected, none throw.
    EXPECT_FALSE(mas::decode_heartbeat({"WORK\nw1\n1"}).has_value());
    EXPECT_FALSE(mas::decode_heartbeat({"HB\nw1"}).has_value());
    EXPECT_FALSE(mas::decode_heartbeat({"HB\nw1\n1\nextra"}).has_value());
    EXPECT_FALSE(mas::decode_heartbeat({"HB\n\n1"}).has_value());
    EXPECT_FALSE(mas::decode_heartbeat({"HB\nw1\nabc"}).has_value());
    EXPECT_FALSE(mas::decode_heartbeat({"HB\nw1\n1x"}).has_value());
    EXPECT_FALSE(mas::decode_heartbeat({"HB\nw1\n-1"}).has_value());
}

TEST(Message, ResultRoundTripCarriesWorkerId) {
    const mas::WorkResult r{"day.csv", 123, 4.5, "w2"};
    const auto d = mas::decode_result(mas::encode(r));
    ASSERT_TRUE(d.has_value());
    EXPECT_EQ(d->in_path, "day.csv");
    EXPECT_EQ(d->events, 123);
    EXPECT_DOUBLE_EQ(d->seconds, 4.5);
    EXPECT_EQ(d->worker_id, "w2");
}

TEST(Message, DecodeResultRejectsMissingOrEmptyWorkerId) {
    // Plan 3's 4-line RESULT frame is no longer valid.
    EXPECT_FALSE(mas::decode_result({"RESULT\nday.csv\n123\n4.5"}).has_value());
    EXPECT_FALSE(mas::decode_result({"RESULT\nday.csv\n123\n4.5\n"}).has_value());
}
```

Then update the file's **existing** RESULT-format tests: every `mas::WorkResult{...}` aggregate stays valid C++ (the new member defaults to `""`), but any existing round-trip that encodes a `WorkResult` without a `worker_id` will now fail decode. Give those literals a worker id (e.g. `mas::WorkResult{"d.csv", 5, 0.1, "w1"}`) and any raw 4-line RESULT payload strings an appended `\nw1` **except** the ones asserting rejection. Keep the trailing-garbage rejection tests exactly as they are (they must still reject).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cmake --build build -j 2>&1 | tail -5 && ./build/unit_tests --gtest_filter='Message.*'`
Expected: compile error — `Heartbeat` not declared / `decode_heartbeat` undefined. That is the RED state for a codec change.

- [ ] **Step 3: Implement**

`core/include/mas/Message.hpp` — replace the `WorkResult` block and extend the API:

```cpp
// Worker -> sink: outcome of one WorkItem. events == -1 => input unreadable
// (mirrors clean_file's contract). worker_id attributes the result to the
// worker that produced it, so the coordinator can write a dead worker's
// store off and re-dispatch its completions (resilience spec §5/§6).
struct WorkResult {
    std::string in_path;
    long long events = 0;
    double seconds = 0.0;
    std::string worker_id;
};

// Worker -> coordinator liveness beacon (resilience spec §5). seq increases
// monotonically per worker; the coordinator only uses arrival time.
struct Heartbeat {
    std::string worker_id;
    long long seq = 0;
};
```

and alongside the existing declarations:

```cpp
Message encode(const Heartbeat& h);
std::optional<Heartbeat> decode_heartbeat(const Message& m);
```

`core/src/Message.cpp` — add the tag next to the others:

```cpp
constexpr const char* kHeartbeatTag = "HB";
```

extend `encode(const WorkResult&)` to append the id:

```cpp
Message encode(const WorkResult& r) {
    return {std::string(kResultTag) + "\n" + r.in_path + "\n" +
            std::to_string(r.events) + "\n" + std::to_string(r.seconds) +
            "\n" + r.worker_id};
}
```

extend `decode_result` (5 fields, non-empty id):

```cpp
std::optional<WorkResult> decode_result(const Message& m) {
    const auto f = split_lines(m.payload);
    if (f.size() != 5 || f[0] != kResultTag || f[4].empty()) return std::nullopt;
    try {
        std::size_t events_end = 0;
        std::size_t seconds_end = 0;
        const long long events = std::stoll(f[2], &events_end);
        const double seconds = std::stod(f[3], &seconds_end);
        // Wire bytes are untrusted: a field with trailing garbage ("5x")
        // still stoll/stod-parses its prefix, so require full consumption.
        if (events_end != f[2].size() || seconds_end != f[3].size()) {
            return std::nullopt;
        }
        return WorkResult{f[1], events, seconds, f[4]};
    } catch (const std::exception&) {
        return std::nullopt;   // non-numeric events/seconds
    }
}
```

and add the heartbeat codec:

```cpp
Message encode(const Heartbeat& h) {
    return {std::string(kHeartbeatTag) + "\n" + h.worker_id + "\n" +
            std::to_string(h.seq)};
}

std::optional<Heartbeat> decode_heartbeat(const Message& m) {
    const auto f = split_lines(m.payload);
    if (f.size() != 3 || f[0] != kHeartbeatTag || f[1].empty()) return std::nullopt;
    try {
        std::size_t seq_end = 0;
        const long long seq = std::stoll(f[2], &seq_end);
        if (seq_end != f[2].size() || seq < 0) return std::nullopt;
        return Heartbeat{f[1], seq};
    } catch (const std::exception&) {
        return std::nullopt;
    }
}
```

**Expect collateral breakage** in `tests/test_cleaning_worker.cpp` (and check `tests/test_zmq_transport.cpp` for any `WorkResult` literals) if any encode call sites relied on 4-line results — they compile (aggregate gains a defaulted member) but `decode_result` of an id-less result now yields nullopt. Do **not** fix CleaningWorker behavior here (Task 2 owns it); only make this task's test file self-consistent. If `test_cleaning_worker.cpp` assertions on decoded results start failing because the worker sends no id yet, adjust those assertions minimally to check `has_value()` expectations that still hold, or mark the exact assertion lines with the fix landing in Task 2 — prefer the smallest edit that keeps the suite green.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cmake --build build -j 2>&1 | tail -3 && ./build/unit_tests`
Expected: all tests PASS (whole suite, not just `Message.*`).

- [ ] **Step 5: Commit**

```bash
git add core/include/mas/Message.hpp core/src/Message.cpp tests/test_message.cpp tests/test_cleaning_worker.cpp
git commit -m "feat(transport): Heartbeat codec + worker_id attribution on WorkResult"
```

---

### Task 2: CleaningWorker — heartbeats, idle-tick self-exit, worker_id stamping

**Files:**
- Modify: `core/include/mas/CleaningWorker.hpp`
- Modify: `core/src/CleaningWorker.cpp`
- Modify: `tests/fakes/FakeTransport.hpp`
- Test: `tests/test_cleaning_worker.cpp`

**Interfaces:**
- Consumes: `Heartbeat`, `encode(Heartbeat)`, `WorkResult.worker_id` from Task 1.
- Produces (Task 6 relies on this exact ctor):
  - `CleaningWorker(IMessageSource& work, IMessageSink& results, IMessageSink& heartbeats, IEventStore& store, std::string worker_id, CleanFn clean_fn);`
  - `static constexpr int kIdleExitTicks = 60;`
  - Behavior contract: one heartbeat at `run()` entry ("hello", prompt registration), one per empty `recv` tick, one after each result; `run()` returns after `kIdleExitTicks` *consecutive* empty ticks; STOP still exits immediately; any received frame (even malformed) resets the idle counter.
- Produces (Task 4's tests reuse): `mas::test::FakeTickSource` — a source whose script may contain `std::nullopt` entries to simulate empty timeout ticks.

- [ ] **Step 1: Add `FakeTickSource` to the fakes header**

Append to `tests/fakes/FakeTransport.hpp` inside `namespace mas::test`:

```cpp
// Scripted source that can interleave empty ticks: a nullopt entry models a
// recv timeout (production ZmqPullSource with a finite timeout). After the
// script drains it reports nullopt forever, like FakeSource.
struct FakeTickSource : IMessageSource {
    std::deque<std::optional<Message>> script;
    std::optional<Message> recv() override {
        if (script.empty()) return std::nullopt;
        std::optional<Message> m = std::move(script.front());
        script.pop_front();
        return m;
    }
};
```

- [ ] **Step 2: Write the failing tests**

Rewrite `tests/test_cleaning_worker.cpp`'s constructions to the new shape and add the new behaviors. The file keeps its existing tests (STOP exit, malformed-frame skip, unreadable-input `-1`) — update each `mas::CleaningWorker worker(work, results, store, fn)` to `mas::CleaningWorker worker(work, results, hb, store, "w1", fn)` with a `mas::test::FakeSink hb;` beside the other fakes. New tests:

```cpp
TEST(CleaningWorker, StampsWorkerIdAndHeartbeatsAroundWork) {
    mas::test::FakeSource work;
    work.queue.push_back(mas::encode(mas::WorkItem{"a.csv"}));
    work.queue.push_back(mas::make_stop());
    mas::test::FakeSink results, hb;
    mas::test::FakeEventStore store;
    mas::CleaningWorker worker(work, results, hb, store, "w7",
        [](const std::string&, mas::IEventStore&) { return 5LL; });

    EXPECT_EQ(worker.run(), 1);

    ASSERT_EQ(results.sent.size(), 1u);
    const auto r = mas::decode_result(results.sent[0]);
    ASSERT_TRUE(r.has_value());
    EXPECT_EQ(r->worker_id, "w7");
    EXPECT_EQ(r->events, 5);
    // hello + one after the result; no empty ticks in this script.
    ASSERT_EQ(hb.sent.size(), 2u);
    for (const auto& m : hb.sent) {
        const auto h = mas::decode_heartbeat(m);
        ASSERT_TRUE(h.has_value());
        EXPECT_EQ(h->worker_id, "w7");
    }
    // seq strictly increases.
    EXPECT_LT(mas::decode_heartbeat(hb.sent[0])->seq,
              mas::decode_heartbeat(hb.sent[1])->seq);
}

TEST(CleaningWorker, HeartbeatsEveryEmptyTickAndExitsAfterBudget) {
    mas::test::FakeSource work;   // empty forever: every recv is an empty tick
    mas::test::FakeSink results, hb;
    mas::test::FakeEventStore store;
    mas::CleaningWorker worker(work, results, hb, store, "w1",
        [](const std::string&, mas::IEventStore&) { return 0LL; });

    EXPECT_EQ(worker.run(), 0);

    // hello + one per empty tick until the budget exhausts.
    EXPECT_EQ(hb.sent.size(),
              1u + static_cast<std::size_t>(mas::CleaningWorker::kIdleExitTicks));
    EXPECT_TRUE(results.sent.empty());
}

TEST(CleaningWorker, IdleCounterResetsWhenWorkArrives) {
    mas::test::FakeTickSource work;
    const int n = mas::CleaningWorker::kIdleExitTicks - 1;
    for (int i = 0; i < n; ++i) work.script.push_back(std::nullopt);
    work.script.push_back(mas::encode(mas::WorkItem{"a.csv"}));
    for (int i = 0; i < n; ++i) work.script.push_back(std::nullopt);
    work.script.push_back(mas::make_stop());
    mas::test::FakeSink results, hb;
    mas::test::FakeEventStore store;
    mas::CleaningWorker worker(work, results, hb, store, "w1",
        [](const std::string&, mas::IEventStore&) { return 1LL; });

    // 2*(budget-1) empty ticks straddle one item: never 60 consecutive,
    // so the worker survives to the STOP and handles the item.
    EXPECT_EQ(worker.run(), 1);
    ASSERT_EQ(results.sent.size(), 1u);
}
```

Note: `FakeEventStore` is whatever in-memory store fake the file already uses — reuse the existing one verbatim (check the top of the current file; do not invent a second fake).

- [ ] **Step 3: Run tests to verify they fail**

Run: `cmake --build build -j 2>&1 | tail -5`
Expected: compile error — `CleaningWorker` has no 6-argument constructor, `kIdleExitTicks` undeclared.

- [ ] **Step 4: Implement**

`core/include/mas/CleaningWorker.hpp`:

```cpp
#pragma once
#include "mas/EventStore.hpp"
#include "mas/Transport.hpp"
#include <functional>
#include <string>

namespace mas {

// Cleaning agent loop (spec §5.3 + resilience spec §5/§7): PULL work items,
// clean each day-file into the injected store, PUSH one WorkResult per item,
// and PUSH heartbeats so the coordinator can tell death from silence.
// Liveness contract: one heartbeat at run() entry, one per empty recv tick
// (production wires a 1 s recv timeout), one after each result. The only
// silent window is while clean_fn runs on one file.
class CleaningWorker {
public:
    using CleanFn = std::function<long long(const std::string&, IEventStore&)>;

    // Consecutive empty ticks before run() gives up on the coordinator and
    // returns (~60 s at the production 1 s tick). Tick counting instead of a
    // clock keeps this deterministic under test fakes.
    static constexpr int kIdleExitTicks = 60;

    CleaningWorker(IMessageSource& work, IMessageSink& results,
                   IMessageSink& heartbeats, IEventStore& store,
                   std::string worker_id, CleanFn clean_fn);

    // Blocks; returns the number of work items handled (failures included —
    // their WorkResult carries events == -1). Exits on STOP or after
    // kIdleExitTicks consecutive empty ticks.
    int run();

private:
    void beat();

    IMessageSource& work_;
    IMessageSink& results_;
    IMessageSink& heartbeats_;
    IEventStore& store_;
    std::string worker_id_;
    CleanFn clean_fn_;
    long long hb_seq_ = 0;
};

} // namespace mas
```

`core/src/CleaningWorker.cpp`:

```cpp
#include "mas/CleaningWorker.hpp"
#include "mas/Message.hpp"
#include <chrono>
#include <utility>

namespace mas {

CleaningWorker::CleaningWorker(IMessageSource& work, IMessageSink& results,
                               IMessageSink& heartbeats, IEventStore& store,
                               std::string worker_id, CleanFn clean_fn)
    : work_(work), results_(results), heartbeats_(heartbeats), store_(store),
      worker_id_(std::move(worker_id)), clean_fn_(std::move(clean_fn)) {}

void CleaningWorker::beat() {
    heartbeats_.send(encode(Heartbeat{worker_id_, hb_seq_++}));
}

int CleaningWorker::run() {
    int handled = 0;
    int idle_ticks = 0;
    beat();   // hello: register with the coordinator promptly
    while (idle_ticks < kIdleExitTicks) {
        const auto msg = work_.recv();
        if (!msg) {   // empty tick: recv timed out (or the source closed)
            ++idle_ticks;
            beat();
            continue;
        }
        idle_ticks = 0;   // any frame proves the coordinator is alive
        if (is_stop(*msg)) break;
        const auto item = decode_work(*msg);
        if (!item) continue;   // malformed frame: drop it, keep serving
        const auto t0 = std::chrono::steady_clock::now();
        const long long events = clean_fn_(item->in_path, store_);
        const std::chrono::duration<double> dt =
            std::chrono::steady_clock::now() - t0;
        results_.send(
            encode(WorkResult{item->in_path, events, dt.count(), worker_id_}));
        beat();
        ++handled;
    }
    return handled;
}

} // namespace mas
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cmake --build build -j 2>&1 | tail -3 && ./build/unit_tests`
Expected: all PASS. Note: any pre-existing worker test that scripted "source dries up → worker exits" now spins through 60 fake empty ticks first — same return value, 60 extra heartbeats; if such a test asserts heartbeat counts, account for the hello + ticks.

- [ ] **Step 6: Commit**

```bash
git add core/include/mas/CleaningWorker.hpp core/src/CleaningWorker.cpp tests/fakes/FakeTransport.hpp tests/test_cleaning_worker.cpp
git commit -m "feat(agent): CleaningWorker heartbeats, idle-tick self-exit, worker_id stamping"
```

---

### Task 3: Coordinator core loop — registry, attribution, STOP × live

**Files:**
- Modify: `core/include/mas/Coordinator.hpp`
- Modify: `core/src/Coordinator.cpp`
- Test: `tests/test_coordinator.cpp`

**Interfaces:**
- Consumes: `Heartbeat`/`decode_heartbeat`, `WorkResult.worker_id` (Task 1); `FakeSource`/`FakeSink` fakes.
- Produces (Task 4 extends the same function; Task 6 wires it):

```cpp
struct DispatchSummary {
    long long total_events = 0;
    int files_ok = 0;
    int files_failed = 0;   // events<0, cap-exceeded, or abort leftovers
    int workers_died = 0;   // tombstoned by the deadline sweep (Task 4)
};

struct CoordinatorConfig {
    std::chrono::milliseconds death_threshold{30000};
    int redispatch_cap = 2;   // re-sends allowed per item beyond the first
};

// Injectable time source so unit tests drive deadlines without sleeping.
using ClockFn = std::function<std::chrono::steady_clock::time_point()>;

DispatchSummary run_coordinator(const std::vector<WorkItem>& items,
                                IMessageSink& work, IMessageSource& results,
                                IMessageSource& heartbeats,
                                const CoordinatorConfig& cfg, ClockFn now);
```

- Task 3 lands: dispatch, heartbeat registration/refresh, result attribution (dup drop, malformed drop, dead-worker drop, `events<0` fail), termination on empty outstanding, STOP × live. Death sweep, re-dispatch, cap, and aborts are Task 4 — the sweep/abort code paths exist but are exercised and completed there.
- **Test-termination invariant (this task only):** every Task 3 test scripts a result set that completes all items, so the loop always reaches `open == 0`. Tests here use a *fixed* clock (`[]{ return std::chrono::steady_clock::time_point{}; }`) so no deadline can fire.

- [ ] **Step 1: Write the failing tests**

Replace `tests/test_coordinator.cpp` wholesale:

```cpp
#include "mas/Coordinator.hpp"
#include "fakes/FakeTransport.hpp"
#include <chrono>
#include <gtest/gtest.h>
#include <vector>

namespace {

using sc = std::chrono::steady_clock;

mas::ClockFn fixed_clock() {
    return [] { return sc::time_point{}; };
}

mas::Message hb(const std::string& id, long long seq) {
    return mas::encode(mas::Heartbeat{id, seq});
}

mas::Message result(const std::string& path, long long events,
                    const std::string& wid) {
    return mas::encode(mas::WorkResult{path, events, 0.1, wid});
}

TEST(Coordinator, DispatchesCollectsAndStopsEachLiveWorker) {
    const std::vector<mas::WorkItem> items = {{"d1.csv"}, {"d2.csv"}, {"d3.csv"}};
    mas::test::FakeSink work;
    mas::test::FakeSource results;
    results.queue.push_back(result("d1.csv", 10, "w1"));
    results.queue.push_back(result("d3.csv", 30, "w2"));
    results.queue.push_back(result("d2.csv", 20, "w1"));
    mas::test::FakeSource hbs;
    hbs.queue.push_back(hb("w1", 0));
    hbs.queue.push_back(hb("w2", 0));

    const auto s = mas::run_coordinator(items, work, results, hbs,
                                        mas::CoordinatorConfig{}, fixed_clock());

    EXPECT_EQ(s.files_ok, 3);
    EXPECT_EQ(s.files_failed, 0);
    EXPECT_EQ(s.total_events, 60);
    EXPECT_EQ(s.workers_died, 0);
    ASSERT_EQ(work.sent.size(), 5u);   // 3 WORK then one STOP per live worker
    for (int i = 0; i < 3; ++i)
        EXPECT_TRUE(mas::decode_work(work.sent[i]).has_value()) << i;
    EXPECT_TRUE(mas::is_stop(work.sent[3]));
    EXPECT_TRUE(mas::is_stop(work.sent[4]));
}

TEST(Coordinator, ResultAloneRegistersItsWorker) {
    // No heartbeat ever arrives; the result itself is the liveness signal
    // and the registry gains w1, which then receives the single STOP.
    const std::vector<mas::WorkItem> items = {{"d1.csv"}};
    mas::test::FakeSink work;
    mas::test::FakeSource results;
    results.queue.push_back(result("d1.csv", 7, "w1"));
    mas::test::FakeSource hbs;

    const auto s = mas::run_coordinator(items, work, results, hbs,
                                        mas::CoordinatorConfig{}, fixed_clock());

    EXPECT_EQ(s.files_ok, 1);
    ASSERT_EQ(work.sent.size(), 2u);
    EXPECT_TRUE(mas::is_stop(work.sent.back()));
}

TEST(Coordinator, UnreadableInputCountsFailedWithoutRedispatch) {
    const std::vector<mas::WorkItem> items = {{"d1.csv"}, {"d2.csv"}};
    mas::test::FakeSink work;
    mas::test::FakeSource results;
    results.queue.push_back(result("d1.csv", -1, "w1"));   // deterministic failure
    results.queue.push_back(result("d2.csv", 20, "w1"));
    mas::test::FakeSource hbs;

    const auto s = mas::run_coordinator(items, work, results, hbs,
                                        mas::CoordinatorConfig{}, fixed_clock());

    EXPECT_EQ(s.files_ok, 1);
    EXPECT_EQ(s.files_failed, 1);
    EXPECT_EQ(s.total_events, 20);
    ASSERT_EQ(work.sent.size(), 3u);   // 2 WORK + 1 STOP; no re-send of d1
}

TEST(Coordinator, DuplicateResultIsDropped) {
    const std::vector<mas::WorkItem> items = {{"d1.csv"}, {"d2.csv"}};
    mas::test::FakeSink work;
    mas::test::FakeSource results;
    results.queue.push_back(result("d1.csv", 10, "w1"));
    results.queue.push_back(result("d1.csv", 999, "w2"));   // late duplicate
    results.queue.push_back(result("d2.csv", 20, "w2"));
    mas::test::FakeSource hbs;

    const auto s = mas::run_coordinator(items, work, results, hbs,
                                        mas::CoordinatorConfig{}, fixed_clock());

    EXPECT_EQ(s.files_ok, 2);
    EXPECT_EQ(s.total_events, 30);   // first result wins; 999 never counted
}

TEST(Coordinator, MalformedAndUnknownPathResultsAreIgnored) {
    const std::vector<mas::WorkItem> items = {{"d1.csv"}};
    mas::test::FakeSink work;
    mas::test::FakeSource results;
    results.queue.push_back(mas::Message{"RESULT\nd1.csv\nnot-a-number\n0.1\nw1"});
    results.queue.push_back(result("nosuch.csv", 5, "w1"));   // path never dispatched
    results.queue.push_back(result("d1.csv", 10, "w1"));
    mas::test::FakeSource hbs;

    const auto s = mas::run_coordinator(items, work, results, hbs,
                                        mas::CoordinatorConfig{}, fixed_clock());

    EXPECT_EQ(s.files_ok, 1);
    EXPECT_EQ(s.files_failed, 0);
    EXPECT_EQ(s.total_events, 10);
}

TEST(Coordinator, NoItemsStopsNobodyWhenRegistryEmpty) {
    mas::test::FakeSink work;
    mas::test::FakeSource results;
    mas::test::FakeSource hbs;
    const auto s = mas::run_coordinator({}, work, results, hbs,
                                        mas::CoordinatorConfig{}, fixed_clock());
    EXPECT_EQ(s.files_ok, 0);
    EXPECT_EQ(s.files_failed, 0);
    // Nothing to do and nobody registered: no WORK, no STOP.
    EXPECT_TRUE(work.sent.empty());
}

} // namespace
```

Semantics pinned by that last test: with zero items the loop body never runs, so nobody registers and no STOP is sent — workers idle out on their tick budget. This replaces Plan 3's `NoItemsStillStopsWorkers` (blind `num_workers` is gone).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cmake --build build -j 2>&1 | tail -5`
Expected: compile error — `run_coordinator` has no 6-argument overload; `CoordinatorConfig`/`ClockFn` undeclared.

- [ ] **Step 3: Implement**

`core/include/mas/Coordinator.hpp`:

```cpp
#pragma once
#include "mas/Message.hpp"
#include "mas/Transport.hpp"
#include <chrono>
#include <functional>
#include <vector>

namespace mas {

struct DispatchSummary {
    long long total_events = 0;
    int files_ok = 0;      // results with events >= 0
    int files_failed = 0;  // events < 0, re-dispatch cap exceeded, or abort leftovers
    int workers_died = 0;  // workers tombstoned by the deadline sweep
};

struct CoordinatorConfig {
    // A worker silent longer than this is declared dead (resilience spec §6).
    std::chrono::milliseconds death_threshold{30000};
    // Re-sends allowed per item beyond its first dispatch; exceeding it marks
    // the item permanently failed (poison-item protection).
    int redispatch_cap = 2;
};

// Injectable time source so unit tests drive deadlines without sleeping.
using ClockFn = std::function<std::chrono::steady_clock::time_point()>;

// Ventilator + sink + liveness monitor in one call (resilience spec §6):
// PUSH every item, then tick: drain heartbeats, take one result per tick
// (the results source's recv timeout paces the loop), sweep deadlines and
// re-dispatch a dead worker's open items and completions, until every item
// is settled. Ends by PUSHing one STOP per live registry entry. The dead
// worker's store file is written off — its rows are recreated in survivor
// stores and the idempotent upsert absorbs any overlap.
DispatchSummary run_coordinator(const std::vector<WorkItem>& items,
                                IMessageSink& work, IMessageSource& results,
                                IMessageSource& heartbeats,
                                const CoordinatorConfig& cfg, ClockFn now);

} // namespace mas
```

`core/src/Coordinator.cpp`:

```cpp
#include "mas/Coordinator.hpp"
#include <iostream>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace mas {
namespace {

struct ItemState {
    int redispatches = 0;   // sends beyond the first
    bool done = false;      // ok, failed, or permanently failed
};

struct WorkerState {
    std::chrono::steady_clock::time_point last_seen{};
    bool alive = true;
    // (in_path, events) of this worker's ok results: reopened if it dies,
    // because its store file is written off (resilience spec §3/§6).
    std::vector<std::pair<std::string, long long>> completed;
};

} // namespace

DispatchSummary run_coordinator(const std::vector<WorkItem>& items,
                                IMessageSink& work, IMessageSource& results,
                                IMessageSource& heartbeats,
                                const CoordinatorConfig& cfg, ClockFn now) {
    DispatchSummary s;
    std::unordered_map<std::string, ItemState> state;   // in_path -> state
    for (const auto& item : items) state.emplace(item.in_path, ItemState{});
    std::size_t open = state.size();   // duplicate paths collapse by contract
    for (const auto& item : items) work.send(encode(item));

    std::unordered_map<std::string, WorkerState> registry;
    const auto start = now();

    const auto touch = [&](const std::string& worker_id) -> WorkerState* {
        auto [it, inserted] = registry.try_emplace(worker_id);
        if (inserted) {
            it->second.last_seen = now();
            std::cerr << "coordinator: worker " << worker_id << " joined\n";
        } else if (!it->second.alive) {
            return nullptr;   // tombstoned: dead is dead (spec §8)
        } else {
            it->second.last_seen = now();
        }
        return &it->second;
    };

    // Loop-pass order is load-bearing for deterministic tests and pinned
    // here: (1) one results tick — its recv timeout paces the loop and, under
    // test fakes, advances virtual time; (2) heartbeat drain — refreshes are
    // stamped with now() AFTER the tick, so a beat delivered this pass
    // survives the deadline this pass; (3) deadline sweep; (4) aborts.
    while (open > 0) {
        // 1) One result tick (production recv timeout 200 ms paces the loop).
        if (const auto msg = results.recv()) {
            const auto r = decode_result(*msg);
            if (!r) {
                std::cerr << "coordinator: dropped malformed result\n";
            } else if (WorkerState* w = touch(r->worker_id); !w) {
                // Late result from a tombstoned worker: its store is written
                // off, so counting this would credit rows we may never merge.
                std::cerr << "coordinator: dropped result for " << r->in_path
                          << " from dead worker " << r->worker_id << "\n";
            } else {
                const auto st = state.find(r->in_path);
                if (st == state.end() || st->second.done) {
                    std::cerr << "coordinator: dropped duplicate/unknown result for "
                              << r->in_path << "\n";
                } else {
                    st->second.done = true;
                    --open;
                    if (r->events >= 0) {
                        ++s.files_ok;
                        s.total_events += r->events;
                        w->completed.emplace_back(r->in_path, r->events);
                    } else {
                        ++s.files_failed;   // unreadable input: deterministic,
                                            // re-dispatch would not help
                    }
                }
            }
        }

        // 2) Heartbeats: drain without blocking (production recv timeout 0).
        while (auto hb_msg = heartbeats.recv()) {
            const auto hb = decode_heartbeat(*hb_msg);
            if (!hb) {
                std::cerr << "coordinator: dropped malformed heartbeat\n";
                continue;
            }
            touch(hb->worker_id);
        }

        // 3) Deadline sweep + re-dispatch (completed in Task 4).
        // 4) Abort conditions (completed in Task 4).
    }

    for (const auto& [id, w] : registry) {
        if (w.alive) work.send(make_stop());
        (void)id;
    }
    return s;
}

} // namespace mas
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cmake --build build -j 2>&1 | tail -3 && ./build/unit_tests`
Expected: all PASS (old coordinator tests were replaced in Step 1; nothing else links `run_coordinator` except `coordinator_main.cpp`, which now **fails to compile** — fix it minimally in this task by updating the call site as shown below, final CLI polish lands in Task 6).

Minimal `coordinator_main.cpp` bridge so the build stays green (temporary wiring, Task 6 rewrites the argv handling):

```cpp
// after constructing `results`, add:
mas::ZmqPullSource heartbeats(ctx, "tcp://127.0.0.1:5559", /*bind=*/true,
                              /*timeout_ms=*/0);
const auto s = mas::run_coordinator(items, work, results, heartbeats,
                                    mas::CoordinatorConfig{},
                                    [] { return std::chrono::steady_clock::now(); });
```

(add `#include <chrono>`; delete the now-unused `num_workers` validation only if it blocks compilation — Task 6 owns the CLI shape).

- [ ] **Step 5: Commit**

```bash
git add core/include/mas/Coordinator.hpp core/src/Coordinator.cpp core/src/coordinator_main.cpp tests/test_coordinator.cpp
git commit -m "feat(agent): coordinator registry + attribution loop, STOP per live worker"
```

---

### Task 4: Death detection, re-dispatch, cap, aborts

**Files:**
- Modify: `core/src/Coordinator.cpp` (sections 3 and 4 of the loop)
- Test: `tests/test_coordinator.cpp`

**Interfaces:**
- Consumes: everything Task 3 produced (same signature — no header change).
- Produces: the complete spec §6 behavior set. Task 6 and the chaos script rely on these stderr markers (exact prefixes): `coordinator: worker <id> joined`, `coordinator: worker <id> dead`, `coordinator: re-dispatch <path>`, `coordinator: item <path> failed permanently`, `coordinator: abort:`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_coordinator.cpp`. First the test-local timed source (top of the anonymous namespace, after the helpers):

```cpp
// Results source that advances a virtual clock per tick: entry (msg, dt)
// advances *t by dt then yields msg (nullopt = an empty 200 ms tick).
struct TimedSource : mas::IMessageSource {
    std::deque<std::pair<std::optional<mas::Message>, std::chrono::milliseconds>>
        script;
    sc::time_point* t = nullptr;
    std::optional<mas::Message> recv() override {
        if (script.empty()) return std::nullopt;
        auto [m, dt] = std::move(script.front());
        script.pop_front();
        *t += dt;
        return m;
    }
};
```

**Scripting discipline these tests rest on** (the loop-pass order was pinned in Task 3's implementation comment): each pass runs *results tick → heartbeat drain → sweep → aborts*. The `TimedSource` on the results channel is the only thing that advances virtual time, and it advances **before** that pass's heartbeat drain — so a beat scripted for pass N refreshes its worker at the *post-jump* time. Heartbeats use `FakeTickSource` (Task 2), whose `nullopt` entries end one pass's drain loop: they are per-pass separators. Every test below is written against exactly that schedule; a worker "dies" when a pass's jump puts its last refresh more than 30 s in the past, and "survives" when the same pass's drain hands it a fresh beat.

Then the behaviors:

```cpp
TEST(Coordinator, SilentWorkerIsDeclaredDeadAndItsWorkRedispatched) {
    using namespace std::chrono_literals;
    const std::vector<mas::WorkItem> items = {{"d1.csv"}, {"d2.csv"}};
    sc::time_point t{};
    mas::test::FakeSink work;
    TimedSource results;
    results.t = &t;
    // pass 1: w1 reports d1 at t=0 (implicit join); drain registers w1, w2.
    // pass 2: empty tick jumps to t=31 s; drain refreshes only w1 -> sweep
    //         tombstones w2 and re-dispatches the open d2.
    // pass 3: w1 completes the re-dispatched d2.
    results.script.push_back({mas::encode(mas::WorkResult{"d1.csv", 10, 0.1, "w1"}), 0ms});
    results.script.push_back({std::nullopt, 31000ms});
    results.script.push_back({mas::encode(mas::WorkResult{"d2.csv", 20, 0.1, "w1"}), 0ms});
    mas::test::FakeTickSource hbs;
    hbs.script.push_back(hb("w1", 0));
    hbs.script.push_back(hb("w2", 0));
    hbs.script.push_back(std::nullopt);   // end pass-1 drain
    hbs.script.push_back(hb("w1", 1));    // pass 2: w1 alive at t=31 s

    const auto s = mas::run_coordinator(items, work, results, hbs,
                                        mas::CoordinatorConfig{},
                                        [&] { return t; });

    EXPECT_EQ(s.files_ok, 2);
    EXPECT_EQ(s.files_failed, 0);
    EXPECT_EQ(s.total_events, 30);
    EXPECT_EQ(s.workers_died, 1);
    // 2 WORK + 1 re-dispatched WORK (d2) + 1 STOP (only w1 lives).
    ASSERT_EQ(work.sent.size(), 4u);
    const auto redispatched = mas::decode_work(work.sent[2]);
    ASSERT_TRUE(redispatched.has_value());
    EXPECT_EQ(redispatched->in_path, "d2.csv");
    EXPECT_TRUE(mas::is_stop(work.sent[3]));
}

TEST(Coordinator, DeadWorkersCompletionsAreReopenedAndRecounted) {
    using namespace std::chrono_literals;
    // w2 completes d2 (99 events), then dies while d1 is still open (the
    // open item keeps the loop alive across the jump). Its store is written
    // off, so d2 reopens (files_ok/total_events roll back) and w1
    // re-completes it. Pass schedule: (1) w2 reports d2, both workers join;
    // (2) jump to t=31 s, w1 refreshes, w2 dies -> d2 reopens and both open
    // items re-dispatch (the extra d1 re-send is harmless — w1 completes it
    // normally); (3) w1 completes d1; (4) w1 completes the re-dispatched d2.
    const std::vector<mas::WorkItem> items = {{"d1.csv"}, {"d2.csv"}};
    sc::time_point t{};
    mas::test::FakeSink work;
    TimedSource results;
    results.t = &t;
    results.script.push_back({mas::encode(mas::WorkResult{"d2.csv", 99, 0.1, "w2"}), 0ms});
    results.script.push_back({std::nullopt, 31000ms});
    results.script.push_back({mas::encode(mas::WorkResult{"d1.csv", 10, 0.1, "w1"}), 0ms});
    results.script.push_back({mas::encode(mas::WorkResult{"d2.csv", 20, 0.1, "w1"}), 0ms});
    mas::test::FakeTickSource hbs;
    hbs.script.push_back(hb("w1", 0));
    hbs.script.push_back(hb("w2", 0));
    hbs.script.push_back(std::nullopt);   // end pass-1 drain
    hbs.script.push_back(hb("w1", 1));    // pass 2: w1 alive at t=31 s

    const auto s = mas::run_coordinator(items, work, results, hbs,
                                        mas::CoordinatorConfig{},
                                        [&] { return t; });

    EXPECT_EQ(s.files_ok, 2);          // d1 + re-completed d2; 99 rolled back
    EXPECT_EQ(s.total_events, 30);     // 10 + 20, never 99
    EXPECT_EQ(s.workers_died, 1);
}

TEST(Coordinator, RedispatchCapMarksItemPermanentlyFailed) {
    using namespace std::chrono_literals;
    // One item, three workers that die one per pass while holding it:
    // initial send + 2 re-dispatches exhaust the cap; the third death
    // permanently fails the item instead of re-sending it.
    const std::vector<mas::WorkItem> items = {{"poison.csv"}};
    sc::time_point t{};
    mas::test::FakeSink work;
    TimedSource results;
    results.t = &t;
    results.script.push_back({std::nullopt, 31000ms});   // pass 1 -> t=31
    results.script.push_back({std::nullopt, 31000ms});   // pass 2 -> t=62
    results.script.push_back({std::nullopt, 31000ms});   // pass 3 -> t=93
    results.script.push_back({std::nullopt, 31000ms});   // pass 4 -> t=124
    mas::test::FakeTickSource hbs;
    // pass 1 (t=31): all three join.
    hbs.script.push_back(hb("w1", 0));
    hbs.script.push_back(hb("w2", 0));
    hbs.script.push_back(hb("w3", 0));
    hbs.script.push_back(std::nullopt);
    // pass 2 (t=62): w2/w3 refresh; w1 (last 31) dies -> re-dispatch #1.
    hbs.script.push_back(hb("w2", 1));
    hbs.script.push_back(hb("w3", 1));
    hbs.script.push_back(std::nullopt);
    // pass 3 (t=93): w3 refreshes; w2 (last 62) dies -> re-dispatch #2.
    hbs.script.push_back(hb("w3", 2));
    hbs.script.push_back(std::nullopt);
    // pass 4 (t=124): nobody beats; w3 (last 93) dies -> cap exceeded.

    const auto s = mas::run_coordinator(items, work, results, hbs,
                                        mas::CoordinatorConfig{},
                                        [&] { return t; });

    EXPECT_EQ(s.files_ok, 0);
    EXPECT_EQ(s.files_failed, 1);
    EXPECT_EQ(s.workers_died, 3);
    // 1 initial WORK + 2 re-dispatches, no third re-send, no STOP (all dead).
    ASSERT_EQ(work.sent.size(), 3u);
    for (const auto& m : work.sent)
        EXPECT_TRUE(mas::decode_work(m).has_value());
}

TEST(Coordinator, AbortsWhenNoWorkerEverJoins) {
    using namespace std::chrono_literals;
    const std::vector<mas::WorkItem> items = {{"d1.csv"}};
    sc::time_point t{};
    mas::test::FakeSink work;
    TimedSource results;
    results.t = &t;
    results.script.push_back({std::nullopt, 31000ms});
    mas::test::FakeTickSource hbs;   // nobody ever heartbeats

    const auto s = mas::run_coordinator(items, work, results, hbs,
                                        mas::CoordinatorConfig{},
                                        [&] { return t; });

    EXPECT_EQ(s.files_ok, 0);
    EXPECT_EQ(s.files_failed, 1);      // abort leftovers count as failed
    ASSERT_EQ(work.sent.size(), 1u);   // the initial WORK only: no STOP
    EXPECT_FALSE(mas::is_stop(work.sent[0]));
}

TEST(Coordinator, AbortsWhenAllWorkersAreDead) {
    using namespace std::chrono_literals;
    const std::vector<mas::WorkItem> items = {{"d1.csv"}, {"d2.csv"}};
    sc::time_point t{};
    mas::test::FakeSink work;
    TimedSource results;
    results.t = &t;
    results.script.push_back({mas::encode(mas::WorkResult{"d1.csv", 10, 0.1, "w1"}), 0ms});
    results.script.push_back({std::nullopt, 31000ms});   // w1 dies; nobody left
    mas::test::FakeTickSource hbs;
    hbs.script.push_back(hb("w1", 0));

    const auto s = mas::run_coordinator(items, work, results, hbs,
                                        mas::CoordinatorConfig{},
                                        [&] { return t; });

    // d1's completion rolls back with w1's death (store written off), d2 was
    // never completed: both count failed; nothing is ok; no STOP goes out.
    EXPECT_EQ(s.files_ok, 0);
    EXPECT_EQ(s.files_failed, 2);
    EXPECT_EQ(s.total_events, 0);
    EXPECT_EQ(s.workers_died, 1);
    for (const auto& m : work.sent) EXPECT_FALSE(mas::is_stop(m));
}

TEST(Coordinator, ZombieHeartbeatDoesNotResurrect) {
    using namespace std::chrono_literals;
    const std::vector<mas::WorkItem> items = {{"d1.csv"}};
    sc::time_point t{};
    mas::test::FakeSink work;
    TimedSource results;
    results.t = &t;
    results.script.push_back({std::nullopt, 0ms});       // pass 1: register at t=0
    results.script.push_back({std::nullopt, 31000ms});   // pass 2: w1 dies
    results.script.push_back({mas::encode(mas::WorkResult{"d1.csv", 10, 0.1, "w2"}), 0ms});
    mas::test::FakeTickSource hbs;
    hbs.script.push_back(hb("w1", 0));
    hbs.script.push_back(hb("w2", 0));
    hbs.script.push_back(std::nullopt);   // end pass-1 drain
    hbs.script.push_back(hb("w2", 1));    // pass 2: w2 alive at t=31 s
    hbs.script.push_back(std::nullopt);
    hbs.script.push_back(hb("w1", 1));    // pass 3: zombie beat, must be ignored

    const auto s = mas::run_coordinator(items, work, results, hbs,
                                        mas::CoordinatorConfig{},
                                        [&] { return t; });

    EXPECT_EQ(s.workers_died, 1);
    EXPECT_EQ(s.files_ok, 1);
    // Exactly one STOP: w2. The zombie w1 must not rejoin the registry.
    int stops = 0;
    for (const auto& m : work.sent) stops += mas::is_stop(m) ? 1 : 0;
    EXPECT_EQ(stops, 1);
}

TEST(Coordinator, LateResultFromTombstonedWorkerIsDropped) {
    using namespace std::chrono_literals;
    const std::vector<mas::WorkItem> items = {{"d1.csv"}};
    sc::time_point t{};
    mas::test::FakeSink work;
    TimedSource results;
    results.t = &t;
    results.script.push_back({std::nullopt, 0ms});       // pass 1: register at t=0
    results.script.push_back({std::nullopt, 31000ms});   // pass 2: w1 dies
    // pass 3: zombie w1 reports d1 — store written off, must not count.
    results.script.push_back({mas::encode(mas::WorkResult{"d1.csv", 99, 0.1, "w1"}), 0ms});
    results.script.push_back({mas::encode(mas::WorkResult{"d1.csv", 10, 0.1, "w2"}), 0ms});
    mas::test::FakeTickSource hbs;
    hbs.script.push_back(hb("w1", 0));
    hbs.script.push_back(hb("w2", 0));
    hbs.script.push_back(std::nullopt);   // end pass-1 drain
    hbs.script.push_back(hb("w2", 1));    // pass 2: w2 survives the jump
    hbs.script.push_back(std::nullopt);
    hbs.script.push_back(hb("w2", 2));    // pass 3: w2 stays alive

    const auto s = mas::run_coordinator(items, work, results, hbs,
                                        mas::CoordinatorConfig{},
                                        [&] { return t; });

    EXPECT_EQ(s.files_ok, 1);
    EXPECT_EQ(s.total_events, 10);   // never 99
    EXPECT_EQ(s.workers_died, 1);
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cmake --build build -j 2>&1 | tail -3 && timeout 30 ./build/unit_tests --gtest_filter='Coordinator.*'; echo "exit=$?"`
(macOS has no GNU `timeout`; any equivalent kill-after-N-seconds wrapper with the exit-124 convention is fine.)
Expected RED: build OK; `SilentWorker...` fails its assertions (no sweep → no re-dispatch → `work.sent` is 3, not 4). The cap/abort tests **hang** without the missing sweep/abort code (the loop spins with `open > 0` on a drained script) — the `timeout 30` kill (`exit=124`) is their expected RED signal, not a test bug.

- [ ] **Step 3: Implement**

Replace the section-3/4 placeholders in `core/src/Coordinator.cpp`'s loop (the pass order — results tick, heartbeat drain, sweep, aborts — is already pinned by Task 3; do not reorder it):

```cpp
        // 3) Deadline sweep: tombstone silent workers, write their stores
        //    off, reopen their completions, re-dispatch every open item.
        const auto t = now();
        bool any_death = false;
        for (auto& [id, w] : registry) {
            if (!w.alive || t - w.last_seen <= cfg.death_threshold) continue;
            w.alive = false;
            any_death = true;
            ++s.workers_died;
            std::cerr << "coordinator: worker " << id << " dead (silent > "
                      << cfg.death_threshold.count() << " ms)\n";
            for (const auto& [path, events] : w.completed) {
                auto st = state.find(path);
                if (st == state.end() || !st->second.done) continue;
                st->second.done = false;   // rows lived only in the dead store
                ++open;
                --s.files_ok;
                s.total_events -= events;
            }
            w.completed.clear();
        }
        if (any_death) {
            for (auto& [path, st] : state) {
                if (st.done) continue;
                if (st.redispatches >= cfg.redispatch_cap) {
                    st.done = true;
                    --open;
                    ++s.files_failed;
                    std::cerr << "coordinator: item " << path
                              << " failed permanently (re-dispatch cap)\n";
                    continue;
                }
                ++st.redispatches;
                std::cerr << "coordinator: re-dispatch " << path << " (attempt "
                          << (st.redispatches + 1) << ")\n";
                work.send(encode(WorkItem{path}));
            }
        }

        // 4) Abort: nobody to do the remaining work.
        int live = 0;
        for (const auto& [id, w] : registry) {
            if (w.alive) ++live;
            (void)id;
        }
        const bool nobody_ever = registry.empty() &&
                                 (t - start > cfg.death_threshold);
        if (open > 0 && ((live == 0 && !registry.empty()) || nobody_ever)) {
            std::cerr << "coordinator: abort: no live workers, " << open
                      << " items unsettled\n";
            s.files_failed += static_cast<int>(open);
            open = 0;   // leave the loop; STOP goes only to live workers (none)
        }
```

Cap semantics pinned: `redispatch_cap = 2` allows two re-sends (three dispatches total); the sweep that *would* re-send a third time permanently fails the item instead.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cmake --build build -j 2>&1 | tail -3 && ./build/unit_tests`
Expected: all PASS, including every Task 3 test unchanged.

- [ ] **Step 5: Commit**

```bash
git add core/src/Coordinator.cpp tests/test_coordinator.cpp
git commit -m "feat(agent): heartbeat death sweep, outstanding-set re-dispatch, cap + aborts"
```

---

### Task 5: `mas_merge` skips unopenable stores

**Files:**
- Modify: `core/src/merge_main.cpp`

**Interfaces:**
- Consumes: `DuckDbEventStore::merge_from` (throws `std::runtime_error`/duckdb exceptions on a bad source — behavior already unit-tested at store level).
- Produces: CLI contract the chaos script relies on — a source that fails to ATTACH prints `skip <path>: <reason>` to stderr and merging continues; exit 0 as long as the destination opens; summary line `merged <n> stores (<k> skipped); dst holds <rows> rows`.

- [ ] **Step 1: Implement (no unit RED here — store-level throw is already covered; this is CLI plumbing verified by smoke + chaos)**

Replace the loop body in `core/src/merge_main.cpp`:

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
        int merged = 0, skipped = 0;
        for (int i = 3; i < argc; ++i) {
            // A crashed worker's store may be unreadable; the resilience
            // design writes it off (its items were re-dispatched), so a
            // failed source is skipped loudly instead of aborting the merge.
            try {
                dst.merge_from(argv[i]);
                ++merged;
            } catch (const std::exception& e) {
                std::cerr << "skip " << argv[i] << ": " << e.what() << "\n";
                ++skipped;
            }
        }
        std::cerr << "merged " << merged << " stores (" << skipped
                  << " skipped); dst holds " << dst.count() << " rows\n";
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << "\n";
        return 1;
    }
    return 0;
}
```

- [ ] **Step 2: Build and smoke-test both paths**

```bash
cmake --build build -j 2>&1 | tail -3
cd /tmp && rm -f m_dst.duckdb m_bad.duckdb
printf 'not a duckdb file' > m_bad.duckdb
"$OLDPWD/build/mas_merge" m_dst.duckdb MCC m_bad.duckdb; echo "exit=$?"
cd "$OLDPWD"
```

Expected stderr: a `skip /.../m_bad.duckdb: ...` line, then `merged 0 stores (1 skipped); dst holds 0 rows`, `exit=0`.

- [ ] **Step 3: Run the full suite (regression only)**

Run: `./build/unit_tests`
Expected: all PASS (nothing links merge_main).

- [ ] **Step 4: Commit**

```bash
git add core/src/merge_main.cpp
git commit -m "feat(cli): mas_merge skips unopenable source stores (crashed-worker write-off)"
```

---

### Task 6: Mains rewiring — new CLIs, timeouts, live smoke

**Files:**
- Modify: `core/src/worker_main.cpp`
- Modify: `core/src/coordinator_main.cpp`

**Interfaces:**
- Consumes: Task 2 ctor, Task 3/4 `run_coordinator`, existing `ZmqPushSink`/`ZmqPullSource` timeout parameters.
- Produces: final CLI contracts (chaos script depends on these exactly):
  - `mas_worker <work_endpoint> <result_endpoint> <hb_endpoint> <out.duckdb> <worker_id> [machine_id]`
  - `mas_coordinator <work_endpoint> <result_endpoint> <hb_endpoint> <day1.csv> [day2.csv ...]`
  - Coordinator exit 0 iff `files_failed == 0`; usage errors exit 2; runtime errors exit 1 (both binaries, unchanged convention).

- [ ] **Step 1: Rewrite `core/src/worker_main.cpp`**

```cpp
#include "mas/CleaningWorker.hpp"
#include "mas/DuckDbEventStore.hpp"
#include "mas/Pipeline.hpp"
#include "mas/ZmqTransport.hpp"
#include <exception>
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    if (argc < 6) {
        std::cerr << "usage: mas_worker <work_endpoint> <result_endpoint> "
                     "<hb_endpoint> <out.duckdb> <worker_id> [machine_id]\n";
        return 2;
    }
    const std::string work_ep = argv[1], result_ep = argv[2], hb_ep = argv[3];
    const std::string out = argv[4], worker_id = argv[5];
    const std::string machine = (argc > 6) ? argv[6] : "MCC";
    try {
        zmq::context_t ctx(1);
        // Liveness (resilience spec §7): the 1 s work recv timeout is the
        // wait tick — each empty tick heartbeats, 60 in a row exits. Send
        // timeouts turn a dead coordinator into a thrown error instead of a
        // forever-blocked PUSH.
        mas::ZmqPullSource work(ctx, work_ep, /*bind=*/false,
                                /*timeout_ms=*/1000);
        mas::ZmqPushSink results(ctx, result_ep, /*bind=*/false,
                                 /*send_timeout_ms=*/60000);
        mas::ZmqPushSink heartbeats(ctx, hb_ep, /*bind=*/false,
                                    /*send_timeout_ms=*/60000);
        mas::DuckDbEventStore store(out, machine);
        mas::CleaningWorker worker(work, results, heartbeats, store, worker_id,
            [](const std::string& path, mas::IEventStore& s) {
                return mas::clean_file(path, s);
            });
        const int handled = worker.run();
        std::cerr << "worker " << worker_id << " done: " << handled
                  << " work items, store holds " << store.count() << " rows\n";
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << "\n";
        return 1;
    }
    return 0;
}
```

- [ ] **Step 2: Rewrite `core/src/coordinator_main.cpp`**

```cpp
#include "mas/Coordinator.hpp"
#include "mas/ZmqTransport.hpp"
#include <chrono>
#include <exception>
#include <iostream>
#include <string>
#include <vector>

int main(int argc, char** argv) {
    if (argc < 5) {
        std::cerr << "usage: mas_coordinator <work_endpoint> <result_endpoint> "
                     "<hb_endpoint> <day1.csv> [day2.csv ...]\n";
        return 2;
    }
    try {
        const std::string work_ep = argv[1], result_ep = argv[2], hb_ep = argv[3];
        std::vector<mas::WorkItem> items;
        for (int i = 4; i < argc; ++i) items.push_back({argv[i]});

        zmq::context_t ctx(1);
        // Send-side liveness: a mute work socket (nobody ever drains it)
        // throws after 60 s instead of hanging this process forever.
        mas::ZmqPushSink work(ctx, work_ep, /*bind=*/true,
                              /*send_timeout_ms=*/60000);
        // 200 ms of results silence = one loop tick: paces heartbeat drains
        // and deadline sweeps (resilience spec §6).
        mas::ZmqPullSource results(ctx, result_ep, /*bind=*/true,
                                   /*timeout_ms=*/200);
        // Heartbeats are drained without blocking each tick.
        mas::ZmqPullSource heartbeats(ctx, hb_ep, /*bind=*/true,
                                      /*timeout_ms=*/0);
        const auto s = mas::run_coordinator(
            items, work, results, heartbeats, mas::CoordinatorConfig{},
            [] { return std::chrono::steady_clock::now(); });
        std::cerr << "dispatched " << items.size() << " files: " << s.files_ok
                  << " ok, " << s.files_failed << " failed, " << s.total_events
                  << " events, " << s.workers_died << " workers died\n";
        return s.files_failed == 0 ? 0 : 1;
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << "\n";
        return 1;
    }
}
```

- [ ] **Step 3: Build + full suite**

Run: `cmake --build build -j 2>&1 | tail -3 && ./build/unit_tests`
Expected: builds clean, all tests PASS.

- [ ] **Step 4: Live 2-process TCP smoke (Plan 3 Task 7 convention)**

```bash
S=/tmp/mas_smoke && rm -rf $S && mkdir -p $S
printf 'ts;Head01_Count\n2026-02-01T00:00:00;1\n2026-02-01T00:00:01;2\n' > $S/tiny.csv
./build/mas_worker tcp://127.0.0.1:5561 tcp://127.0.0.1:5562 tcp://127.0.0.1:5563 $S/w1.duckdb w1 &
./build/mas_coordinator tcp://127.0.0.1:5561 tcp://127.0.0.1:5562 tcp://127.0.0.1:5563 $S/tiny.csv
echo "coordinator exit=$?"
wait
```

**Adapt the tiny CSV header to the real column naming used by earlier smokes** — check what Plan 3's Task 7 smoke used (see `docs/validation-log.md` or `tests/test_pipeline.cpp` fixtures) and reuse that exact shape; the point is only: coordinator exits 0, prints `1 ok, 0 failed`, `0 workers died`; worker prints `worker w1 done: 1 work items`. Also smoke the liveness fix: start `mas_worker` alone against unused endpoints with a 5 s `timeout` guard is wrong (budget is 60 s) — instead just note the worker now exits by itself after ~60 s where Plan 3's hung forever; the chaos script asserts coordinator-death behavior properly.

- [ ] **Step 5: Commit**

```bash
git add core/src/worker_main.cpp core/src/coordinator_main.cpp
git commit -m "feat(cli): heartbeat endpoint + liveness timeouts in mas_worker/mas_coordinator"
```

---

### Task 7: Chaos E2E script + validation log

**Files:**
- Create: `scripts/chaos_e2e.sh`
- Modify: `docs/validation-log.md` (append entry)

**Interfaces:**
- Consumes: final CLIs (Task 6), `mas_merge` skip behavior (Task 5), coordinator stderr markers (Task 4), real day-file CSVs from `telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02/` (local, gitignored), `clean` CLI (Plan 2) as the per-file oracle.
- Produces: committed script + logged evidence. Success criteria (spec §11): coordinator exit 0 with one worker killed mid-run; merged survivor count == sum of per-file single-process counts; coordinator log shows one death + ≥1 re-dispatch.

- [ ] **Step 1: Write `scripts/chaos_e2e.sh`**

```bash
#!/usr/bin/env bash
# Chaos E2E (resilience spec §10): run 2 workers on real day-files, kill -9
# one mid-run, assert the run completes and merged counts match the oracle.
# usage: scripts/chaos_e2e.sh <day1.csv> <day2.csv> [day3.csv ...]
set -euo pipefail

[ $# -ge 2 ] || { echo "usage: $0 <day1.csv> <day2.csv> [more.csv ...]"; exit 2; }
BUILD="${BUILD_DIR:-build}"
for exe in mas_coordinator mas_worker mas_merge clean; do
    [ -x "$BUILD/$exe" ] || { echo "missing $BUILD/$exe (build first)"; exit 2; }
done

T="$(mktemp -d /tmp/mas_chaos.XXXXXX)"
PIDS=()
cleanup() {
    for p in "${PIDS[@]:-}"; do kill -9 "$p" 2>/dev/null || true; done
    rm -rf "$T"
}
trap cleanup EXIT

WORK=tcp://127.0.0.1:5571 RES=tcp://127.0.0.1:5572 HB=tcp://127.0.0.1:5573

# Oracle: single-process count per file (clean CLI validated vs the Python
# oracle in Plans 1-2), summed.
EXPECTED=0
for f in "$@"; do
    rm -f "$T/oracle.duckdb"
    n="$("$BUILD/clean" "$f" "$T/oracle.duckdb" 2>/dev/null | tail -1)"
    case "$n" in (*[!0-9]*|"") echo "oracle count failed for $f: '$n'"; exit 1;; esac
    EXPECTED=$((EXPECTED + n))
done
echo "oracle total: $EXPECTED events"

"$BUILD/mas_coordinator" "$WORK" "$RES" "$HB" "$@" 2>"$T/coord.log" &
COORD=$!
PIDS+=("$COORD")
"$BUILD/mas_worker" "$WORK" "$RES" "$HB" "$T/w1.duckdb" w1 2>"$T/w1.log" &
W1=$!
PIDS+=("$W1")
"$BUILD/mas_worker" "$WORK" "$RES" "$HB" "$T/w2.duckdb" w2 2>"$T/w2.log" &
W2=$!
PIDS+=("$W2")

sleep 2   # mid-first-file for real day-files (2-7 s each)
kill -9 "$W1"
echo "killed w1 (pid $W1) at t+2s"

# Coordinator must finish on its own: death detection (30 s) + re-dispatch
# + remaining work. Watchdog well above worst case.
WATCHDOG=300
for _ in $(seq "$WATCHDOG"); do
    kill -0 "$COORD" 2>/dev/null || break
    sleep 1
done
if kill -0 "$COORD" 2>/dev/null; then
    echo "FAIL: coordinator still running after ${WATCHDOG}s"; exit 1
fi
wait "$COORD"; COORD_EXIT=$?
wait "$W2" 2>/dev/null || true

echo "--- coordinator log ---"; cat "$T/coord.log"
[ "$COORD_EXIT" -eq 0 ] || { echo "FAIL: coordinator exit $COORD_EXIT"; exit 1; }
grep -q "dead" "$T/coord.log" || { echo "FAIL: no death detected"; exit 1; }
grep -q "re-dispatch" "$T/coord.log" || { echo "FAIL: no re-dispatch"; exit 1; }

# Merge every store, the written-off one included: intact -> harmless
# idempotent duplicates; corrupt -> mas_merge skips it loudly (Task 5).
"$BUILD/mas_merge" "$T/merged.duckdb" MCC777eda3db57348ef8a3113a642ae74db \
    "$T/w1.duckdb" "$T/w2.duckdb" 2>"$T/merge.log" || { cat "$T/merge.log"; exit 1; }
cat "$T/merge.log"
ROWS="$(sed -n 's/.*dst holds \([0-9]*\) rows.*/\1/p' "$T/merge.log")"

if [ "$ROWS" = "$EXPECTED" ]; then
    echo "PASS: merged $ROWS == oracle $EXPECTED (one worker killed mid-run)"
else
    echo "FAIL: merged $ROWS != oracle $EXPECTED"; exit 1
fi
```

Then: `chmod +x scripts/chaos_e2e.sh`.

Two verification notes for the implementer:
1. The `clean` CLI's stdout/stderr format — confirm how the event count is emitted (see `core/src/clean_main.cpp`) and fix the `n=` extraction to match exactly; same for the `mas_merge` summary regex against Task 5's real output line. Do this by *running* them once, not by guessing.
2. If `wait "$COORD"` races the loop's `kill -0` (already-reaped), `COORD_EXIT` capture via `wait` still works because the shell holds the status of background jobs; keep the sequence as written.

- [ ] **Step 2: Run it on real data**

```bash
D=telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02
scripts/chaos_e2e.sh "$D"/*2026-02-01*.csv "$D"/*2026-02-02*.csv "$D"/*2026-02-03*.csv
```

(Adjust the globs to the actual day-file names — `ls "$D" | head` first. Use ≥3 files so the surviving worker demonstrably absorbs re-dispatched work.)

Expected output tail: coordinator log showing `worker w1 joined`, `worker w2 joined`, `worker w1 dead`, `re-dispatch ...`, summary `... 0 failed ...  1 workers died`; then `PASS: merged <N> == oracle <N>`.

Also run the coordinator-death direction once (spec §11 criterion 2):

```bash
"$BUILD"/mas_worker tcp://127.0.0.1:5581 tcp://127.0.0.1:5582 tcp://127.0.0.1:5583 /tmp/w_orphan.duckdb w1 &
sleep 65 && kill -0 $! 2>/dev/null && echo "FAIL: worker still alive" || echo "PASS: worker self-exited"
```

Expected: `PASS: worker self-exited` (idle budget 60 ticks × 1 s), worker stderr shows `worker w1 done: 0 work items`.

- [ ] **Step 3: Append the evidence to `docs/validation-log.md`**

New section following the existing entry format: date, commands run verbatim, coordinator log excerpt (join/dead/re-dispatch lines + summary), merge output, oracle totals, PASS lines for both directions (worker-kill and coordinator-death), wall-clock. State explicitly which files were used and that `w1.duckdb` merged-or-skipped (whichever happened) without affecting the count.

- [ ] **Step 4: Full suite one last time**

Run: `./build/unit_tests`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/chaos_e2e.sh docs/validation-log.md
git commit -m "test(e2e): chaos script — kill a worker mid-run, oracle-exact after re-dispatch"
```

---

## Plan Self-Review (performed at writing time)

- **Spec coverage:** §4 topology → T6; §5 codec/cadence → T1/T2; §6 coordinator (registration, attribution, sweep, re-dispatch set = open ∪ dead-completions via reopen, cap, aborts, STOP×live, logs) → T3/T4; §7 worker (tick recv, idle exit, send timeouts, hb, merge skip) → T2/T5/T6; §8 edge cases → T3/T4 tests (dup drop, zombie tombstone, dead-result drop, poison cap, all-dead abort, malformed drop) + T7 (crash store); §9 defaults → constants in T2/T6; §10 tests → T1–T4 units, T7 chaos; §11 criteria → T7 both directions. Gap check: spec §5 "one heartbeat before each blocking wait" is realized as hello-at-entry + per-empty-tick (a strict superset of the spec's liveness guarantee — documented in T2's header comment).
- **Placeholders:** none — every step carries code or exact commands; the two "confirm by running" notes in T7 are verification instructions, not deferred design.
- **Type consistency:** `CleaningWorker(work, results, heartbeats, store, worker_id, clean_fn)` used identically in T2/T6; `run_coordinator(items, work, results, heartbeats, cfg, now)` identical in T3/T4/T6; `WorkResult{path, events, seconds, worker_id}` aggregate order fixed by T1 and used in that order everywhere; stderr markers in T4 match the greps in T7.
- **Determinism of fake-time tests:** the coordinator loop's pass order (results tick → heartbeat drain → sweep → aborts) is pinned in T3's implementation and every T4 test script was hand-traced against it pass-by-pass (registration times, refresh times, death pass, re-dispatch counts, STOP counts). If an implementer changes the pass order, T4's tests will fail — that is intended; restore the pinned order rather than editing the tests.
- **Execution-time correction (2026-07-09):** the original `DeadWorkersCompletionsAreReopenedAndRecounted` script completed both items before the time jump, so the loop exited at `open == 0` before the death pass could run — caught by the Task 4 implementer, verified by the controller, and fixed by reordering the script (d2's completion first, jump second, d1's completion third) with assertions unchanged. The version above is the corrected script.
