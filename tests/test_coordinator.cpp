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

mas::Message claim(const std::string& path, const std::string& wid) {
    return mas::encode(mas::WorkClaim{path, wid});
}

mas::Message bye(const std::string& wid) {
    return mas::encode(mas::Goodbye{wid});
}

// Results source that advances a virtual clock per tick: entry (msg, dt)
// advances *t by dt then yields msg (nullopt = an empty 200 ms tick).
struct TimedSource : mas::IMessageSource {
    std::deque<std::pair<std::optional<mas::Message>, std::chrono::milliseconds>>
        script;
    sc::time_point* t = nullptr;
    // The 200 ms is the recv timeout the production ZmqPullSource is given, and
    // an exhausted script keeps modelling it rather than freezing time. Frozen,
    // an item that never settles could never be reached by the death sweep or
    // the abort check either, so a coordinator regression that drops a
    // re-dispatch entirely made the run spin instead of failing -- and gtest has
    // no per-test timeout, so CI hung rather than reporting. Every test here
    // reaches open == 0 before its script drains, so advancing costs them
    // nothing.
    std::optional<mas::Message> recv() override {
        if (script.empty()) {
            *t += std::chrono::milliseconds(200);
            return std::nullopt;
        }
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

TEST(Coordinator, APartialStoreFailureIsRetriedRatherThanCountedFailed) {
    // events == -2 is "the store died with rows already written". Unlike -1 it
    // is not deterministic -- the same file on a healthy store completes -- and
    // the upsert is idempotent on (machine_id, head_id, ts), so re-running it
    // finishes the item instead of duplicating rows. Charged against the
    // re-dispatch cap like any other retry, so a genuinely poisoned item still
    // terminates.
    const std::vector<mas::WorkItem> items = {{"d1.csv"}};
    mas::test::FakeSink work;
    mas::test::FakeSource results;
    results.queue.push_back(result("d1.csv", -2, "w1"));   // partial, retryable
    results.queue.push_back(result("d1.csv", 7, "w1"));    // the retry completes it
    mas::test::FakeSource hbs;
    hbs.queue.push_back(hb("w1", 0));

    const auto s = mas::run_coordinator(items, work, results, hbs,
                                        mas::CoordinatorConfig{}, fixed_clock());

    EXPECT_EQ(s.files_ok, 1);
    EXPECT_EQ(s.files_failed, 0);
    EXPECT_EQ(s.total_events, 7);
    ASSERT_EQ(work.sent.size(), 3u);   // dispatch, re-dispatch, one STOP
    EXPECT_TRUE(mas::decode_work(work.sent[1]).has_value());
}

TEST(Coordinator, RepeatedPartialFailuresStopAtTheRedispatchCap) {
    // A store that fails every time must not be retried forever: same cap that
    // bounds death-driven re-dispatch bounds this.
    const std::vector<mas::WorkItem> items = {{"d1.csv"}};
    mas::test::FakeSink work;
    mas::test::FakeSource results;
    for (int i = 0; i < 4; ++i)
        results.queue.push_back(result("d1.csv", -2, "w1"));
    mas::test::FakeSource hbs;
    hbs.queue.push_back(hb("w1", 0));

    mas::CoordinatorConfig cfg{};        // redispatch_cap = 2
    const auto s = mas::run_coordinator(items, work, results, hbs, cfg,
                                        fixed_clock());

    EXPECT_EQ(s.files_ok, 0);
    EXPECT_EQ(s.files_failed, 1);
    ASSERT_EQ(work.sent.size(), 4u);   // dispatch + 2 retries + one STOP
}

TEST(Coordinator, AnUndeliverableRetrySettlesTheItemInsteadOfLosingTheRun) {
    // The re-dispatch send is the one place work is placed on a socket that is
    // not the initial dispatch, and it can throw for the same reason the STOP
    // fan-out can: ZmqPushSink::send throws on a mute-socket timeout. Unguarded
    // it escaped run_coordinator, so a run whose other items were all settled
    // reported "error:" and exit 1 -- the bug the STOP guard already fixed, at
    // a second site.
    //
    // Settling the item as failed is the honest outcome, not a silent success:
    // it had already failed once with -2, and an undeliverable retry leaves it
    // exactly where it was before the retry was attempted.
    const std::vector<mas::WorkItem> items = {{"d1.csv"}, {"d2.csv"}};
    mas::test::ThrowingSink work;
    work.fail_after = 2;                 // both dispatches land; the retry does not
    mas::test::FakeSource results;
    results.queue.push_back(result("d2.csv", 9, "w1"));
    results.queue.push_back(result("d1.csv", -2, "w1"));   // partial, retryable
    mas::test::FakeSource hbs;
    hbs.queue.push_back(hb("w1", 0));

    mas::DispatchSummary s{};
    ASSERT_NO_THROW(s = mas::run_coordinator(items, work, results, hbs,
                                             mas::CoordinatorConfig{},
                                             fixed_clock()));

    EXPECT_EQ(s.files_ok, 1);            // d2 is not lost with the run
    EXPECT_EQ(s.files_failed, 1);        // d1 settles failed, it does not hang
    EXPECT_EQ(s.total_events, 9);
    EXPECT_EQ(work.sent.size(), 2u);     // the two dispatches, nothing after
}

TEST(Coordinator, AnUndeliverableDeathRedispatchAlsoSettlesInsteadOfLosingTheRun) {
    using namespace std::chrono_literals;
    // The -2 retry is not the only re-dispatch, and it was not the only
    // unguarded one: the death sweep, the departed-holder path and the
    // tombstoned-claim path all place work on the same socket for the same
    // reason. Guarding one of five is what let this bug exist at four sites at
    // once, so the policy now lives in try_redispatch and this test pins the
    // death-sweep site specifically -- a different code path from the -2 test,
    // reached through the sweep rather than through a result.
    //
    // w2 claims d2 and goes silent; the sweep tombstones it and tries to
    // re-place d2 on a socket that has stopped accepting. d1, already
    // completed by w1, must survive with the run.
    const std::vector<mas::WorkItem> items = {{"d1.csv"}, {"d2.csv"}};
    sc::time_point t{};
    mas::test::ThrowingSink work;
    work.fail_after = 2;                  // both initial dispatches land
    TimedSource results;
    results.t = &t;
    // pass 1: w2 claims d2 at t=0; the drain registers w1 and w2.
    // pass 2: w1 completes d1, still at t=0.
    // pass 3: an empty tick jumps to t=31 s and the drain refreshes only w1,
    //         so the sweep tombstones w2 alone and re-places its claimed d2.
    results.script.push_back({claim("d2.csv", "w2"), 0ms});
    results.script.push_back({result("d1.csv", 11, "w1"), 0ms});
    results.script.push_back({std::nullopt, 31000ms});   // w2 goes silent
    mas::test::FakeTickSource hbs;
    hbs.script.push_back(hb("w1", 0));
    hbs.script.push_back(hb("w2", 0));
    hbs.script.push_back(std::nullopt);   // end pass-1 drain
    hbs.script.push_back(hb("w1", 1));
    hbs.script.push_back(std::nullopt);   // end pass-2 drain
    hbs.script.push_back(hb("w1", 2));    // w1 still alive at t=31 s

    mas::DispatchSummary s{};
    ASSERT_NO_THROW(s = mas::run_coordinator(items, work, results, hbs,
                                             mas::CoordinatorConfig{},
                                             [&] { return t; }));

    EXPECT_EQ(s.files_ok, 1);             // d1 is not lost with the run
    EXPECT_EQ(s.files_failed, 1);         // d2 settles once, and only once
    EXPECT_EQ(s.total_events, 11);
    EXPECT_EQ(s.workers_died, 1);
    EXPECT_EQ(work.sent.size(), 2u);      // the two dispatches, nothing after
}

TEST(Coordinator, AnUndeliverableDepartedHolderRedispatchSettlesTheItem) {
    // Site 3 of 5: a worker announces an idle exit while still holding an item.
    // Departure is not death, so nothing is charged and no store is written
    // off -- but the item must still be re-placed, and that send can fail like
    // any other. Pinned separately because all five sites were unguarded once
    // and only two of them had a test.
    const std::vector<mas::WorkItem> items = {{"d1.csv"}, {"d2.csv"}};
    mas::test::ThrowingSink work;
    work.fail_after = 2;                  // both dispatches land; the re-place does not
    mas::test::FakeSource results;
    results.queue.push_back(claim("d2.csv", "w2"));
    results.queue.push_back(result("d1.csv", 11, "w1"));
    results.queue.push_back(bye("w2"));   // departs still holding d2
    mas::test::FakeSource hbs;
    hbs.queue.push_back(hb("w1", 0));

    mas::DispatchSummary s{};
    ASSERT_NO_THROW(s = mas::run_coordinator(items, work, results, hbs,
                                             mas::CoordinatorConfig{},
                                             fixed_clock()));

    EXPECT_EQ(s.files_ok, 1);             // d1 survives with the run
    EXPECT_EQ(s.files_failed, 1);         // d2 settles once
    EXPECT_EQ(s.total_events, 11);
    EXPECT_EQ(s.workers_died, 0);         // an announced exit is not a death
    EXPECT_EQ(work.sent.size(), 2u);
}

TEST(Coordinator, AnUndeliverableUnclaimedDeathRedispatchSettlesTheItem) {
    using namespace std::chrono_literals;
    // Site 4 of 5: an item nobody claimed, at a death. It may be queued in the
    // dead worker's pipe, so it is re-placed uncharged -- the dead worker
    // vouched for nothing. That send is guarded too.
    const std::vector<mas::WorkItem> items = {{"d1.csv"}, {"d2.csv"}};
    sc::time_point t{};
    mas::test::ThrowingSink work;
    work.fail_after = 2;
    TimedSource results;
    results.t = &t;
    results.script.push_back({result("d1.csv", 11, "w1"), 0ms});
    results.script.push_back({std::nullopt, 31000ms});   // w2 goes silent
    mas::test::FakeTickSource hbs;
    hbs.script.push_back(hb("w1", 0));
    hbs.script.push_back(hb("w2", 0));
    hbs.script.push_back(std::nullopt);
    hbs.script.push_back(hb("w1", 1));                   // w1 alive at t=31 s

    mas::DispatchSummary s{};
    ASSERT_NO_THROW(s = mas::run_coordinator(items, work, results, hbs,
                                             mas::CoordinatorConfig{},
                                             [&] { return t; }));

    EXPECT_EQ(s.files_ok, 1);
    EXPECT_EQ(s.files_failed, 1);         // d2, unclaimed, settles once
    EXPECT_EQ(s.workers_died, 1);
    EXPECT_EQ(work.sent.size(), 2u);
}

TEST(Coordinator, AnUndeliverableZombieClaimRedispatchSettlesTheItem) {
    using namespace std::chrono_literals;
    // Site 5 of 5: a CLAIM arriving from a worker already tombstoned for
    // silence. The zombie is alive enough to have taken the item, and its
    // RESULT will be dropped at the same gate, so the item is re-placed
    // uncharged or it never settles at all. Here the death sweep's own
    // re-dispatch lands (send 3) and it is the zombie's claim that cannot.
    const std::vector<mas::WorkItem> items = {{"d1.csv"}, {"d2.csv"}};
    sc::time_point t{};
    mas::test::ThrowingSink work;
    work.fail_after = 3;                  // 2 dispatches + the sweep's re-place
    TimedSource results;
    results.t = &t;
    results.script.push_back({claim("d2.csv", "w2"), 0ms});
    results.script.push_back({result("d1.csv", 11, "w1"), 0ms});
    results.script.push_back({std::nullopt, 31000ms});   // sweep tombstones w2
    results.script.push_back({claim("d2.csv", "w2"), 0ms});   // the zombie speaks
    mas::test::FakeTickSource hbs;
    hbs.script.push_back(hb("w1", 0));
    hbs.script.push_back(hb("w2", 0));
    hbs.script.push_back(std::nullopt);
    hbs.script.push_back(hb("w1", 1));
    hbs.script.push_back(std::nullopt);
    hbs.script.push_back(hb("w1", 2));

    mas::DispatchSummary s{};
    ASSERT_NO_THROW(s = mas::run_coordinator(items, work, results, hbs,
                                             mas::CoordinatorConfig{},
                                             [&] { return t; }));

    EXPECT_EQ(s.files_ok, 1);
    EXPECT_EQ(s.files_failed, 1);
    EXPECT_EQ(s.workers_died, 1);
    EXPECT_EQ(work.sent.size(), 3u);      // the throw is the fourth send
}

TEST(Coordinator, AnUndeliverableFirstDispatchStillFailsTheRun) {
    // The other half of the asymmetry, and the one nothing pinned: dispatch is
    // deliberately NOT best-effort. A work socket that cannot accept the work
    // has produced no run at all, and reporting a summary for it would be the
    // inverse lie. This test fails if a future over-broad try/catch swallows
    // the dispatch path along with the retry and STOP paths.
    const std::vector<mas::WorkItem> items = {{"d1.csv"}};
    mas::test::ThrowingSink work;
    work.fail_after = 0;                 // the very first dispatch throws
    mas::test::FakeSource results;
    mas::test::FakeSource hbs;

    // An advancing clock, not fixed_clock(): if the throw is ever swallowed, no
    // work was placed and no result can arrive, so on a frozen clock the
    // coordinator's "nobody ever registered" abort can never elapse and this
    // test hangs instead of failing. One virtual second per tick lets that
    // abort fire, so a regression here is a red test rather than a stuck CI.
    sc::time_point now{};
    const mas::ClockFn ticking = [now]() mutable {
        now += std::chrono::seconds(1);
        return now;
    };

    EXPECT_THROW(mas::run_coordinator(items, work, results, hbs,
                                      mas::CoordinatorConfig{}, ticking),
                 std::runtime_error);
}

TEST(Coordinator, AnUnreadableInputIsStillNotRetried) {
    // -1 keeps its shortcut: nothing was written, the failure is deterministic,
    // and re-running it would only fail again.
    const std::vector<mas::WorkItem> items = {{"d1.csv"}};
    mas::test::FakeSink work;
    mas::test::FakeSource results;
    results.queue.push_back(result("d1.csv", -1, "w1"));
    mas::test::FakeSource hbs;
    hbs.queue.push_back(hb("w1", 0));

    const auto s = mas::run_coordinator(items, work, results, hbs,
                                        mas::CoordinatorConfig{}, fixed_clock());

    EXPECT_EQ(s.files_failed, 1);
    ASSERT_EQ(work.sent.size(), 2u);   // dispatch, one STOP -- no retry
}

TEST(Coordinator, ATransportFailureAtShutdownStillReportsTheRun) {
    // Every other test in this file runs on FakeSink, whose send cannot fail.
    // The real one can: ZmqPushSink::send throws when a mute socket hits its
    // send timeout, and coordinator_main gives that socket a 60 s one. The
    // likeliest moment is the STOP fan-out, because the peers are on their way
    // out by then -- and a throw there escaped run_coordinator entirely, so the
    // "dispatched N files" summary never printed and the process exited 1 on a
    // run whose events were already settled and persisted.
    //
    // The STOP fan-out is best-effort by construction: the comment at the send
    // site says a surplus STOP is free, and an undelivered one costs a worker
    // its 60-tick idle-exit budget, not a row. Dispatch is not best-effort and
    // still throws -- a work socket that cannot accept the work is a failed run.
    const std::vector<mas::WorkItem> items = {{"d1.csv"}};
    mas::test::ThrowingSink work;
    work.fail_after = 1;                    // the dispatch lands; the STOP does not
    mas::test::FakeSource results;
    results.queue.push_back(result("d1.csv", 42, "w1"));
    mas::test::FakeSource hbs;
    hbs.queue.push_back(hb("w1", 0));

    mas::DispatchSummary s{};
    ASSERT_NO_THROW(s = mas::run_coordinator(items, work, results, hbs,
                                             mas::CoordinatorConfig{},
                                             fixed_clock()));

    EXPECT_EQ(s.files_ok, 1);
    EXPECT_EQ(s.files_failed, 0);
    EXPECT_EQ(s.total_events, 42);
    ASSERT_EQ(work.sent.size(), 1u);        // the WORK frame, and no STOP
    EXPECT_TRUE(mas::decode_work(work.sent[0]).has_value());
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
    // The poison-item story the cap exists for: an item that kills every
    // worker that CLAIMS it. w1..w3 each claim it and die; the cap charges
    // each holder-death, so the third exhausts it and the item fails
    // permanently -- while bystander w4, alive and heartbeating throughout,
    // is untouched and receives its STOP. (Before claims existed, the cap was
    // consumed by *any* death, so three unrelated deaths failed every open
    // item on the run.)
    const std::vector<mas::WorkItem> items = {{"poison.csv"}};
    sc::time_point t{};
    mas::test::FakeSink work;
    TimedSource results;
    results.t = &t;
    results.script.push_back({claim("poison.csv", "w1"), 0ms});   // pass 1
    results.script.push_back({std::nullopt, 31000ms});            // pass 2 -> t=31
    results.script.push_back({claim("poison.csv", "w2"), 0ms});   // pass 3
    results.script.push_back({std::nullopt, 31000ms});            // pass 4 -> t=62
    results.script.push_back({claim("poison.csv", "w3"), 0ms});   // pass 5
    results.script.push_back({std::nullopt, 31000ms});            // pass 6 -> t=93
    mas::test::FakeTickSource hbs;
    // pass 1 (t=0): all four join.
    hbs.script.push_back(hb("w1", 0));
    hbs.script.push_back(hb("w2", 0));
    hbs.script.push_back(hb("w3", 0));
    hbs.script.push_back(hb("w4", 0));
    hbs.script.push_back(std::nullopt);
    // pass 2 (t=31): w2/w3/w4 refresh; w1 (holder, last 0) dies -> charge #1.
    hbs.script.push_back(hb("w2", 1));
    hbs.script.push_back(hb("w3", 1));
    hbs.script.push_back(hb("w4", 1));
    hbs.script.push_back(std::nullopt);
    // pass 3: w2's claim arrives.
    hbs.script.push_back(std::nullopt);
    // pass 4 (t=62): w3/w4 refresh; w2 (holder, last 31) dies -> charge #2.
    hbs.script.push_back(hb("w3", 2));
    hbs.script.push_back(hb("w4", 2));
    hbs.script.push_back(std::nullopt);
    // pass 5: w3's claim arrives.
    hbs.script.push_back(std::nullopt);
    // pass 6 (t=93): w4 refreshes; w3 (holder, last 62) dies -> cap exceeded.
    hbs.script.push_back(hb("w4", 3));
    hbs.script.push_back(std::nullopt);

    const auto s = mas::run_coordinator(items, work, results, hbs,
                                        mas::CoordinatorConfig{},
                                        [&] { return t; });

    EXPECT_EQ(s.files_ok, 0);
    EXPECT_EQ(s.files_failed, 1);
    EXPECT_EQ(s.workers_died, 3);
    // 1 initial WORK + 2 re-dispatches + 4 STOPs (live w4 + 3 tombstones).
    ASSERT_EQ(work.sent.size(), 7u);
    for (int i = 0; i < 3; ++i)
        EXPECT_TRUE(mas::decode_work(work.sent[i]).has_value()) << i;
    for (int i = 3; i < 7; ++i) EXPECT_TRUE(mas::is_stop(work.sent[i])) << i;
}

TEST(Coordinator, LiveWorkersResultSurvivesUnrelatedDeaths) {
    using namespace std::chrono_literals;
    // The failure that motivated claims: wlive holds the only item and
    // finishes it correctly, while three other workers fall silent one pass
    // after another. Charging the item's cap for those deaths permanently
    // failed it, and wlive's correct result was then dropped as a duplicate:
    // a successful run reported as a total loss.
    const std::vector<mas::WorkItem> items = {{"d1.csv"}};
    sc::time_point t{};
    mas::test::FakeSink work;
    TimedSource results;
    results.t = &t;
    results.script.push_back({claim("d1.csv", "wlive"), 0ms});    // pass 1
    results.script.push_back({std::nullopt, 31000ms});            // pass 2 -> t=31: w1 dies
    results.script.push_back({std::nullopt, 31000ms});            // pass 3 -> t=62: w2 dies
    results.script.push_back({std::nullopt, 31000ms});            // pass 4 -> t=93: w3 dies
    results.script.push_back({result("d1.csv", 42, "wlive"), 0ms});
    mas::test::FakeTickSource hbs;
    hbs.script.push_back(hb("wlive", 0));
    hbs.script.push_back(hb("w1", 0));
    hbs.script.push_back(hb("w2", 0));
    hbs.script.push_back(hb("w3", 0));
    hbs.script.push_back(std::nullopt);
    hbs.script.push_back(hb("wlive", 1));
    hbs.script.push_back(hb("w2", 1));
    hbs.script.push_back(hb("w3", 1));
    hbs.script.push_back(std::nullopt);
    hbs.script.push_back(hb("wlive", 2));
    hbs.script.push_back(hb("w3", 2));
    hbs.script.push_back(std::nullopt);
    hbs.script.push_back(hb("wlive", 3));
    hbs.script.push_back(std::nullopt);

    const auto s = mas::run_coordinator(items, work, results, hbs,
                                        mas::CoordinatorConfig{},
                                        [&] { return t; });

    EXPECT_EQ(s.files_ok, 1);
    EXPECT_EQ(s.files_failed, 0);
    EXPECT_EQ(s.total_events, 42);
    EXPECT_EQ(s.workers_died, 3);
    // No re-dispatch at all: the item's holder never died. 1 WORK + 4 STOPs.
    ASSERT_EQ(work.sent.size(), 5u);
    EXPECT_TRUE(mas::decode_work(work.sent[0]).has_value());
    for (int i = 1; i < 5; ++i) EXPECT_TRUE(mas::is_stop(work.sent[i])) << i;
}

TEST(Coordinator, GoodbyeIsDepartureNotDeath) {
    using namespace std::chrono_literals;
    // w1 completes d1, announces its idle-exit, and falls silent -- as every
    // worker that runs out of work does. Silence after a goodbye must not
    // tombstone it: its store is intact, so d1 stays counted, nothing is
    // re-dispatched, and workers_died stays 0.
    const std::vector<mas::WorkItem> items = {{"d1.csv"}, {"d2.csv"}};
    sc::time_point t{};
    mas::test::FakeSink work;
    TimedSource results;
    results.t = &t;
    results.script.push_back({claim("d1.csv", "w1"), 0ms});
    results.script.push_back({result("d1.csv", 10, "w1"), 0ms});
    results.script.push_back({bye("w1"), 0ms});
    results.script.push_back({std::nullopt, 31000ms});   // w1 silent long past the threshold
    results.script.push_back({claim("d2.csv", "w2"), 0ms});
    results.script.push_back({result("d2.csv", 20, "w2"), 0ms});
    mas::test::FakeTickSource hbs;
    hbs.script.push_back(hb("w1", 0));
    hbs.script.push_back(hb("w2", 0));
    hbs.script.push_back(std::nullopt);
    hbs.script.push_back(std::nullopt);
    hbs.script.push_back(std::nullopt);
    hbs.script.push_back(hb("w2", 1));
    hbs.script.push_back(std::nullopt);

    const auto s = mas::run_coordinator(items, work, results, hbs,
                                        mas::CoordinatorConfig{},
                                        [&] { return t; });

    EXPECT_EQ(s.files_ok, 2);
    EXPECT_EQ(s.files_failed, 0);
    EXPECT_EQ(s.total_events, 30);     // d1's 10 events were never rolled back
    EXPECT_EQ(s.workers_died, 0);
    // 2 WORK, no re-dispatch, then STOPs (w2 live; a surplus one for the
    // departed w1 is dropped at the closed pipe).
    ASSERT_EQ(work.sent.size(), 4u);
    EXPECT_TRUE(mas::decode_work(work.sent[0]).has_value());
    EXPECT_TRUE(mas::decode_work(work.sent[1]).has_value());
    EXPECT_TRUE(mas::is_stop(work.sent[2]));
    EXPECT_TRUE(mas::is_stop(work.sent[3]));
}

TEST(Coordinator, DeathRedispatchesOnlyTheDeadWorkersItems) {
    using namespace std::chrono_literals;
    // w1 holds d1, w2 holds d2. w1's death re-dispatches d1 alone: d2's
    // holder is alive and mid-file, and re-sending it would spend a second
    // worker's time producing a result that gets dropped as a duplicate.
    const std::vector<mas::WorkItem> items = {{"d1.csv"}, {"d2.csv"}};
    sc::time_point t{};
    mas::test::FakeSink work;
    TimedSource results;
    results.t = &t;
    results.script.push_back({claim("d1.csv", "w1"), 0ms});       // pass 1
    results.script.push_back({claim("d2.csv", "w2"), 0ms});       // pass 2
    results.script.push_back({std::nullopt, 31000ms});            // pass 3 -> t=31: w1 dies
    results.script.push_back({result("d2.csv", 20, "w2"), 0ms});
    results.script.push_back({claim("d1.csv", "w2"), 0ms});
    results.script.push_back({result("d1.csv", 10, "w2"), 0ms});
    mas::test::FakeTickSource hbs;
    hbs.script.push_back(hb("w1", 0));
    hbs.script.push_back(hb("w2", 0));
    hbs.script.push_back(std::nullopt);
    hbs.script.push_back(std::nullopt);
    hbs.script.push_back(hb("w2", 1));
    hbs.script.push_back(std::nullopt);

    const auto s = mas::run_coordinator(items, work, results, hbs,
                                        mas::CoordinatorConfig{},
                                        [&] { return t; });

    EXPECT_EQ(s.files_ok, 2);
    EXPECT_EQ(s.total_events, 30);
    EXPECT_EQ(s.workers_died, 1);
    // d1, d2, re-dispatched d1 (and ONLY d1), STOP x2 (live w2 + tomb w1).
    ASSERT_EQ(work.sent.size(), 5u);
    const auto resent = mas::decode_work(work.sent[2]);
    ASSERT_TRUE(resent.has_value());
    EXPECT_EQ(resent->in_path, "d1.csv");
    EXPECT_TRUE(mas::is_stop(work.sent[3]));
    EXPECT_TRUE(mas::is_stop(work.sent[4]));
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

TEST(Coordinator, ZombieClaimRedispatchesTheItemUncharged) {
    using namespace std::chrono_literals;
    // The tombstoned-but-alive absorption case: w2 is tombstoned for silence,
    // but its pipe is still attached, so the anonymous PUSH re-dispatch can
    // land back in its queue. It cleans the file and sends CLAIM + RESULT;
    // both used to be dropped at the touch() gate, no further death occurred,
    // nothing re-dispatched again -- survivors ran dry, idled out, and the
    // run aborted reporting a file failed that was cleaned correctly. A CLAIM
    // from a tombstoned worker is proof it is alive: re-dispatch (uncharged)
    // at the drop site.
    const std::vector<mas::WorkItem> items = {{"d1.csv"}, {"d2.csv"}};
    sc::time_point t{};
    mas::test::FakeSink work;
    TimedSource results;
    results.t = &t;
    // pass 1: w1 reports d1 (implicit join); drain registers w1, w2.
    // pass 2: empty tick jumps to t=31 s; only w1 beats -> w2 tombstoned,
    //         open d2 unclaimed -> re-dispatched (uncharged, death sweep).
    // pass 3: the re-dispatch round-robins into ZOMBIE w2's pipe: its CLAIM
    //         arrives -> the new drop-site re-dispatch (uncharged).
    // pass 4: this copy reaches w1, which claims it...
    // pass 5: ...and completes it. (w2's own RESULT for d2 would be dropped
    //         at the tombstone gate; LateResultFromTombstonedWorkerIsDropped
    //         already pins that.)
    results.script.push_back({mas::encode(mas::WorkResult{"d1.csv", 10, 0.1, "w1"}), 0ms});
    results.script.push_back({std::nullopt, 31000ms});
    results.script.push_back({claim("d2.csv", "w2"), 0ms});
    results.script.push_back({claim("d2.csv", "w1"), 0ms});
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
    EXPECT_EQ(s.files_failed, 0) << "the zombie-claimed file must not fail";
    EXPECT_EQ(s.total_events, 30);
    EXPECT_EQ(s.workers_died, 1);
    // 2 WORK + death-sweep re-dispatch + zombie-claim re-dispatch + 2 STOP.
    ASSERT_EQ(work.sent.size(), 6u);
    const auto d = mas::decode_work(work.sent[3]);
    ASSERT_TRUE(d.has_value()) << "zombie claim must re-emit the item";
    EXPECT_EQ(d->in_path, "d2.csv");
    EXPECT_TRUE(mas::is_stop(work.sent[4]));
    EXPECT_TRUE(mas::is_stop(work.sent[5]));
}

TEST(Coordinator, ZombieClaimForAnItemHeldByALiveWorkerIsNotRedispatched) {
    using namespace std::chrono_literals;
    // The guard on the drop-site re-dispatch: if a live worker already holds
    // the item, the zombie's claim is a stale copy from before its death --
    // re-emitting would duplicate in-flight work for no benefit.
    const std::vector<mas::WorkItem> items = {{"d1.csv"}};
    sc::time_point t{};
    mas::test::FakeSink work;
    TimedSource results;
    results.t = &t;
    // pass 1: w2 claims d1 (implicit join); drain registers w1, w2.
    // pass 2: 31 s of silence from w2 -> tombstoned; d1's holder died ->
    //         charged re-dispatch, holder erased.
    // pass 3: w1 claims the re-dispatched d1 (live holder).
    // pass 4: zombie w2's claim for d1 arrives -> guard: held by live w1,
    //         no re-dispatch.
    // pass 5: w1 completes d1.
    results.script.push_back({claim("d1.csv", "w2"), 0ms});
    results.script.push_back({std::nullopt, 31000ms});
    results.script.push_back({claim("d1.csv", "w1"), 0ms});
    results.script.push_back({claim("d1.csv", "w2"), 0ms});
    results.script.push_back({mas::encode(mas::WorkResult{"d1.csv", 10, 0.1, "w1"}), 0ms});
    mas::test::FakeTickSource hbs;
    hbs.script.push_back(hb("w1", 0));
    hbs.script.push_back(hb("w2", 0));
    hbs.script.push_back(std::nullopt);
    hbs.script.push_back(hb("w1", 1));

    const auto s = mas::run_coordinator(items, work, results, hbs,
                                        mas::CoordinatorConfig{},
                                        [&] { return t; });

    EXPECT_EQ(s.files_ok, 1);
    EXPECT_EQ(s.workers_died, 1);
    // 1 WORK + holder-death re-dispatch + 2 STOP; NO zombie-claim re-emit.
    ASSERT_EQ(work.sent.size(), 4u);
    EXPECT_TRUE(mas::is_stop(work.sent[2]));
    EXPECT_TRUE(mas::is_stop(work.sent[3]));
}

TEST(Coordinator, GoodbyeWithAClaimStillOutstandingRedispatchesIt) {
    // The protocol slip mark_departed defends against (a worker BYEs between
    // CLAIM and RESULT): lines that had never executed under the suite --
    // the departed worker's holder entry is erased and the open item is
    // re-dispatched to the survivors, uncharged.
    const std::vector<mas::WorkItem> items = {{"d1.csv"}, {"d2.csv"}};
    mas::test::FakeSink work;
    mas::test::FakeSource results;
    results.queue.push_back(claim("d1.csv", "w2"));
    results.queue.push_back(bye("w2"));                    // BYE with d1 open
    results.queue.push_back(claim("d1.csv", "w1"));
    results.queue.push_back(result("d1.csv", 10, "w1"));
    results.queue.push_back(result("d2.csv", 20, "w1"));
    mas::test::FakeSource hbs;
    hbs.queue.push_back(hb("w1", 0));
    hbs.queue.push_back(hb("w2", 0));

    const auto s = mas::run_coordinator(items, work, results, hbs,
                                        mas::CoordinatorConfig{}, fixed_clock());

    EXPECT_EQ(s.files_ok, 2);
    EXPECT_EQ(s.files_failed, 0);
    EXPECT_EQ(s.workers_died, 0) << "a goodbye is a departure, not a death";
    EXPECT_EQ(s.total_events, 30);
    // 2 WORK + the holder-departed re-dispatch + STOP x (1 live + 1 departed).
    ASSERT_EQ(work.sent.size(), 5u);
    const auto re = mas::decode_work(work.sent[2]);
    ASSERT_TRUE(re.has_value());
    EXPECT_EQ(re->in_path, "d1.csv");
}
