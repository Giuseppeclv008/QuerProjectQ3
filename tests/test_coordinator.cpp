#include "mas/agent/Coordinator.hpp"
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
    // 2 WORK + 1 re-dispatched WORK (d2) + 2 STOP.
    //
    // Two, not one. w2 was tombstoned for silence, but silence is not proof of
    // death — that is exactly why its item was re-dispatched instead of failed.
    // If w2 is in fact alive its pipe is still attached, and PUSH round-robin
    // would hand it the single STOP meant for w1, leaving the live worker to sit
    // out its ~60 s idle-exit. One STOP per registered worker closes that.
    ASSERT_EQ(work.sent.size(), 5u);
    const auto redispatched = mas::decode_work(work.sent[2]);
    ASSERT_TRUE(redispatched.has_value());
    EXPECT_EQ(redispatched->in_path, "d2.csv");
    EXPECT_TRUE(mas::is_stop(work.sent[3]));
    EXPECT_TRUE(mas::is_stop(work.sent[4]));
}

TEST(Coordinator, DeadWorkersCompletionsAreReopenedAndRecounted) {
    using namespace std::chrono_literals;
    // w2 completes d2 (99 events), then dies. Its store is written off, so
    // d2 reopens (files_ok/total_events roll back) and w1 re-completes it.
    const std::vector<mas::WorkItem> items = {{"d1.csv"}, {"d2.csv"}};
    sc::time_point t{};
    mas::test::FakeSink work;
    TimedSource results;
    results.t = &t;
    // pass 1: w2 reports d2 at t=0 (implicit join); drain registers w1, w2.
    // pass 2: empty tick jumps to t=31 s; drain refreshes only w1 -> sweep
    //         tombstones w2, reopens d2, re-dispatches both open items.
    // pass 3: w1 completes d1.
    // pass 4: w1 completes the re-dispatched d2.
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
    // Two STOPs: one for the live w2, one for w1's registry slot. The zombie w1
    // must not rejoin the registry (that is what this test guards), but it does
    // keep its tombstoned slot, and shutdown now sends a STOP per slot so a
    // still-attached zombie pipe cannot swallow the live worker's.
    int stops = 0;
    for (const auto& m : work.sent) stops += mas::is_stop(m) ? 1 : 0;
    EXPECT_EQ(stops, 2);
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

// --- Final-review fix wave (Plan 4) ---

TEST(Coordinator, SilentForExactlyDeathThresholdIsNotDeclaredDead) {
    using namespace std::chrono_literals;
    // Boundary pin: the sweep compares with `>` against death_threshold, not
    // `>=`. w1 registers at t=0; the next tick jumps virtual time by exactly
    // death_threshold (30000 ms, the CoordinatorConfig default) with no
    // refresh in between. Right at the boundary the worker must survive
    // (workers_died stays 0), and its still-open item completes normally
    // in the pass after.
    const std::vector<mas::WorkItem> items = {{"d1.csv"}};
    sc::time_point t{};
    mas::test::FakeSink work;
    TimedSource results;
    results.t = &t;
    // pass 1: empty tick at t=0; drain registers w1.
    // pass 2: empty tick jumps to t=30000 ms (== death_threshold exactly);
    //         drain refreshes nobody -> sweep must NOT tombstone w1.
    // pass 3: w1 completes d1 normally.
    results.script.push_back({std::nullopt, 0ms});
    results.script.push_back({std::nullopt, 30000ms});
    results.script.push_back({mas::encode(mas::WorkResult{"d1.csv", 10, 0.1, "w1"}), 0ms});
    mas::test::FakeTickSource hbs;
    hbs.script.push_back(hb("w1", 0));
    hbs.script.push_back(std::nullopt);   // end pass-1 drain
    // pass 2: nothing scripted -> FakeTickSource reports an empty tick.

    const auto s = mas::run_coordinator(items, work, results, hbs,
                                        mas::CoordinatorConfig{},
                                        [&] { return t; });

    EXPECT_EQ(s.workers_died, 0);
    EXPECT_EQ(s.files_ok, 1);
    EXPECT_EQ(s.files_failed, 0);
    EXPECT_EQ(s.total_events, 10);
    // 1 WORK + 1 STOP; no re-dispatch, since w1 was never declared dead.
    ASSERT_EQ(work.sent.size(), 2u);
    EXPECT_TRUE(mas::is_stop(work.sent[1]));
}

TEST(Coordinator, MalformedHeartbeatAheadOfValidBeatsIsDroppedAtLoopLevel) {
    // Pins the "coordinator: dropped malformed heartbeat" branch at loop
    // level: a garbage frame ahead of a valid beat in the very same drain
    // must be dropped with `continue` (not a `break` that would abandon the
    // rest of the drain), so the valid beat right behind it still registers
    // the worker in the same pass and the run completes normally.
    const std::vector<mas::WorkItem> items = {{"d1.csv"}};
    mas::test::FakeSink work;
    mas::test::FakeSource results;
    results.queue.push_back(result("d1.csv", 10, "w1"));
    mas::test::FakeSource hbs;
    hbs.queue.push_back(mas::Message{"garbage"});   // malformed: not "HB\n<id>\n<seq>"
    hbs.queue.push_back(hb("w1", 0));

    const auto s = mas::run_coordinator(items, work, results, hbs,
                                        mas::CoordinatorConfig{}, fixed_clock());

    EXPECT_EQ(s.files_ok, 1);
    EXPECT_EQ(s.files_failed, 0);
    EXPECT_EQ(s.total_events, 10);
    ASSERT_EQ(work.sent.size(), 2u);   // 1 WORK + 1 STOP: w1 registered fine
    EXPECT_TRUE(mas::is_stop(work.sent.back()));
}

// --- Registration gate (Plan 5): fix for the PUSH slow-joiner capture ---

TEST(Coordinator, GatedDispatchWaitsForExpectedWorkers) {
    const std::vector<mas::WorkItem> items = {{"d1.csv"}, {"d2.csv"}};
    mas::CoordinatorConfig cfg;
    cfg.expected_workers = 2;
    mas::test::FakeSink work;
    // The gate's tick order is results-then-heartbeats, same as the main
    // loop, and its first results.recv() runs unconditionally every
    // iteration. A plain always-ready FakeSource would therefore hand the
    // gate the first *dispatch-phase* result (meant for the main loop, after
    // WORK goes out) and it would be silently dropped as pre-dispatch
    // liveness noise, permanently losing it. FakeTickSource's leading
    // nullopt models "nothing arrived yet" for that first tick, so the gate
    // is satisfied by the two hellos alone, exactly as intended.
    mas::test::FakeTickSource results;
    results.script.push_back(std::nullopt);
    results.script.push_back(result("d1.csv", 10, "w1"));
    results.script.push_back(result("d2.csv", 20, "w2"));
    mas::test::FakeSource hbs;
    hbs.queue.push_back(hb("w1", 0));
    hbs.queue.push_back(hb("w2", 0));

    const auto s = mas::run_coordinator(items, work, results, hbs, cfg,
                                        fixed_clock());

    EXPECT_EQ(s.files_ok, 2);
    EXPECT_EQ(s.files_failed, 0);
    EXPECT_EQ(s.total_events, 30);
    EXPECT_EQ(s.workers_died, 0);
    // Gate consumes the hellos in its first HB drain, then dispatches:
    // 2 WORK (round-robin over both registered pipes) + 2 STOP.
    ASSERT_EQ(work.sent.size(), 4u);
    EXPECT_TRUE(mas::decode_work(work.sent[0]).has_value());
    EXPECT_TRUE(mas::decode_work(work.sent[1]).has_value());
    EXPECT_TRUE(mas::is_stop(work.sent[2]));
    EXPECT_TRUE(mas::is_stop(work.sent[3]));
}

TEST(Coordinator, GateAbortsWhenNobodyRegisters) {
    using namespace std::chrono_literals;
    const std::vector<mas::WorkItem> items = {{"d1.csv"}};
    mas::CoordinatorConfig cfg;
    cfg.expected_workers = 2;
    sc::time_point t{};
    mas::test::FakeSink work;
    TimedSource results;
    results.t = &t;
    // Two empty ticks of 6000 ms each cross the 10 s registration_timeout
    // (t: 0 -> 6000 -> 12000) with nobody ever registering.
    results.script.push_back({std::nullopt, 6000ms});
    results.script.push_back({std::nullopt, 6000ms});
    mas::test::FakeTickSource hbs;   // nobody ever heartbeats

    const auto s = mas::run_coordinator(items, work, results, hbs, cfg,
                                        [&] { return t; });

    EXPECT_EQ(s.files_ok, 0);
    EXPECT_EQ(s.files_failed, 1);
    // Discriminator: pre-fix code had already PUSHed the item before the
    // registry could ever be checked. The gate must abort before dispatch.
    EXPECT_TRUE(work.sent.empty());
}

TEST(Coordinator, GateProceedsDegradedAfterTimeout) {
    using namespace std::chrono_literals;
    const std::vector<mas::WorkItem> items = {{"d1.csv"}};
    mas::CoordinatorConfig cfg;
    cfg.expected_workers = 2;
    sc::time_point t{};
    mas::test::FakeSink work;
    TimedSource results;
    results.t = &t;
    // pass 1: empty tick jumps t to 11000 ms, crossing the 10 s
    //         registration_timeout; the same pass's HB drain (below)
    //         registers w1 first, so the timeout check sees a non-empty
    //         registry and proceeds degraded (1 of 2) instead of aborting.
    // pass 2 (main loop): w1 completes d1.
    results.script.push_back({std::nullopt, 11000ms});
    results.script.push_back({result("d1.csv", 10, "w1"), 0ms});
    mas::test::FakeTickSource hbs;
    hbs.script.push_back(hb("w1", 0));

    const auto s = mas::run_coordinator(items, work, results, hbs, cfg,
                                        [&] { return t; });

    EXPECT_EQ(s.files_ok, 1);
    EXPECT_EQ(s.files_failed, 0);
    EXPECT_EQ(s.total_events, 10);
    // 1 WORK (degraded dispatch, only w1 ever registered) + 1 STOP.
    ASSERT_EQ(work.sent.size(), 2u);
    EXPECT_TRUE(mas::decode_work(work.sent[0]).has_value());
    EXPECT_TRUE(mas::is_stop(work.sent[1]));
}

} // namespace
