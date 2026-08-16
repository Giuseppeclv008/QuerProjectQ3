#include "mas/agent/CleaningWorker.hpp"
#include "mas/agent/Message.hpp"
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
    mas::test::FakeSink results, hb;
    FakeStore store;
    std::vector<std::string> cleaned;
    mas::CleaningWorker w(work, results, hb, store, "w1",
        [&](const std::string& path, mas::IEventStore&,
            const std::function<void()>&) -> long long {
            cleaned.push_back(path);
            return path == "day1.csv" ? 10 : 20;
        });

    EXPECT_EQ(w.run(), 2);
    EXPECT_EQ(cleaned, (std::vector<std::string>{"day1.csv", "day2.csv"}));
    // claim/result pairs on one socket, claim first (per-pipe FIFO is what
    // guarantees the coordinator learns the holder before the outcome). A STOP
    // exit is the coordinator's own shutdown: no goodbye.
    ASSERT_EQ(results.sent.size(), 4u);
    const auto c0 = mas::decode_claim(results.sent[0]);
    ASSERT_TRUE(c0.has_value());
    EXPECT_EQ(c0->in_path, "day1.csv");
    EXPECT_EQ(c0->worker_id, "w1");
    const auto r0 = mas::decode_result(results.sent[1]);
    ASSERT_TRUE(r0.has_value());
    EXPECT_EQ(r0->in_path, "day1.csv");
    EXPECT_EQ(r0->events, 10);
    EXPECT_EQ(r0->worker_id, "w1");
    const auto c1 = mas::decode_claim(results.sent[2]);
    ASSERT_TRUE(c1.has_value());
    EXPECT_EQ(c1->in_path, "day2.csv");
    const auto r1 = mas::decode_result(results.sent[3]);
    ASSERT_TRUE(r1.has_value());
    EXPECT_EQ(r1->in_path, "day2.csv");
    EXPECT_EQ(r1->events, 20);
    EXPECT_EQ(r1->worker_id, "w1");
}

TEST(CleaningWorker, MalformedWorkItemIsSkippedWithoutResult) {
    mas::test::FakeSource work;
    work.queue.push_back(mas::Message{"garbage"});
    work.queue.push_back(mas::encode(mas::WorkItem{"day1.csv"}));
    work.queue.push_back(mas::make_stop());
    mas::test::FakeSink results, hb;
    FakeStore store;
    mas::CleaningWorker w(work, results, hb, store, "w1",
        [](const std::string&, mas::IEventStore&,
           const std::function<void()>&) -> long long { return 7; });
    EXPECT_EQ(w.run(), 1);
    // A malformed frame produces neither claim nor result.
    ASSERT_EQ(results.sent.size(), 2u);
    EXPECT_TRUE(mas::decode_claim(results.sent[0]).has_value());
    const auto r = mas::decode_result(results.sent[1]);
    ASSERT_TRUE(r.has_value());
    EXPECT_EQ(r->in_path, "day1.csv");
    EXPECT_EQ(r->events, 7);
    EXPECT_EQ(r->worker_id, "w1");
}

TEST(CleaningWorker, UnreadableInputForwardsMinusOne) {
    mas::test::FakeSource work;
    work.queue.push_back(mas::encode(mas::WorkItem{"missing.csv"}));
    work.queue.push_back(mas::make_stop());
    mas::test::FakeSink results, hb;
    FakeStore store;
    mas::CleaningWorker w(work, results, hb, store, "w1",
        [](const std::string&, mas::IEventStore&,
           const std::function<void()>&) -> long long { return -1; });
    EXPECT_EQ(w.run(), 1);
    ASSERT_EQ(results.sent.size(), 2u);   // claim + failed result
    const auto r = mas::decode_result(results.sent[1]);
    ASSERT_TRUE(r.has_value());
    EXPECT_EQ(r->in_path, "missing.csv");
    EXPECT_EQ(r->events, -1);
    EXPECT_EQ(r->worker_id, "w1");
}

TEST(CleaningWorker, StampsWorkerIdAndHeartbeatsAroundWork) {
    mas::test::FakeSource work;
    work.queue.push_back(mas::encode(mas::WorkItem{"a.csv"}));
    work.queue.push_back(mas::make_stop());
    mas::test::FakeSink results, hb;
    FakeStore store;
    mas::CleaningWorker worker(work, results, hb, store, "w7",
        [](const std::string&, mas::IEventStore&,
           const std::function<void()>&) { return 5LL; });

    EXPECT_EQ(worker.run(), 1);

    ASSERT_EQ(results.sent.size(), 2u);   // claim + result
    const auto r = mas::decode_result(results.sent[1]);
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
    FakeStore store;
    mas::CleaningWorker worker(work, results, hb, store, "w1",
        [](const std::string&, mas::IEventStore&,
           const std::function<void()>&) { return 0LL; });

    EXPECT_EQ(worker.run(), 0);

    // hello + one per empty tick until the budget exhausts.
    EXPECT_EQ(hb.sent.size(),
              1u + static_cast<std::size_t>(mas::CleaningWorker::kIdleExitTicks));
    // An idle exit is voluntary and announced: exactly one goodbye, so the
    // coordinator can tell "left" from "died" and neither reopens this
    // worker's completions nor burns any item's re-dispatch budget on it.
    ASSERT_EQ(results.sent.size(), 1u);
    const auto bye = mas::decode_goodbye(results.sent[0]);
    ASSERT_TRUE(bye.has_value());
    EXPECT_EQ(bye->worker_id, "w1");
}

TEST(CleaningWorker, IdleCounterResetsWhenWorkArrives) {
    mas::test::FakeTickSource work;
    const int n = mas::CleaningWorker::kIdleExitTicks - 1;
    for (int i = 0; i < n; ++i) work.script.push_back(std::nullopt);
    work.script.push_back(mas::encode(mas::WorkItem{"a.csv"}));
    for (int i = 0; i < n; ++i) work.script.push_back(std::nullopt);
    work.script.push_back(mas::make_stop());
    mas::test::FakeSink results, hb;
    FakeStore store;
    mas::CleaningWorker worker(work, results, hb, store, "w1",
        [](const std::string&, mas::IEventStore&,
           const std::function<void()>&) { return 1LL; });

    // 2*(budget-1) empty ticks straddle one item: never 60 consecutive,
    // so the worker survives to the STOP and handles the item.
    EXPECT_EQ(worker.run(), 1);
    ASSERT_EQ(results.sent.size(), 2u);   // claim + result; STOP exit: no goodbye
}

TEST(CleaningWorker, CleanFnReceivesABeatCallbackItCanUse) {
    // The parquet path supplies its own store, so it must be handed the beat
    // to decorate it with. If CleanFn stops carrying one, that path goes
    // silent for a whole file and the coordinator tombstones a live worker.
    mas::test::FakeSource work;
    mas::test::FakeSink results, hbs;
    FakeStore store;
    work.queue.push_back(mas::encode(mas::WorkItem{"d1.csv"}));
    // STOP right after the one item: run() terminates deterministically
    // instead of idling out through kIdleExitTicks empty-tick beats, which
    // would pad hbs.sent and hide a clean_fn that ignores the beat callback
    // it's handed.
    work.queue.push_back(mas::make_stop());
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
    // hello + the fn's beat + the post-result beat -- exact, not a lower
    // bound, so a clean_fn that swallows the callback instead of calling it
    // makes this fail rather than pass on idle-tick beats alone.
    EXPECT_EQ(hbs.sent.size(), 3u);
}

TEST(CleaningWorker, AThrowingCleanFnFailsTheItemNotTheWorker) {
    // A store exception (ROLLBACK; throw) or a beat() send timeout used to
    // unwind past main: the process exited with NO result sent, the
    // coordinator burned the 30 s death threshold, tombstoned a worker whose
    // only sin was one bad item, and re-dispatched everything it held. The
    // events == -1 failure channel existed and nothing routed into it.
    mas::test::FakeSource work;
    work.queue.push_back(mas::encode(mas::WorkItem{"bad.csv"}));
    work.queue.push_back(mas::encode(mas::WorkItem{"good.csv"}));
    work.queue.push_back(mas::make_stop());
    mas::test::FakeSink results, hb;
    FakeStore store;
    mas::CleaningWorker w(work, results, hb, store, "w1",
        [](const std::string& path, mas::IEventStore&,
           const std::function<void()>&) -> long long {
            if (path == "bad.csv") throw std::runtime_error("TransactionContext Error");
            return 5;
        });

    EXPECT_EQ(w.run(), 2);   // both items handled; the worker outlives the throw
    ASSERT_EQ(results.sent.size(), 4u);
    const auto bad = mas::decode_result(results.sent[1]);
    ASSERT_TRUE(bad.has_value());
    EXPECT_EQ(bad->in_path, "bad.csv");
    EXPECT_EQ(bad->events, -1);
    const auto good = mas::decode_result(results.sent[3]);
    ASSERT_TRUE(good.has_value());
    EXPECT_EQ(good->in_path, "good.csv");
    EXPECT_EQ(good->events, 5);
}

TEST(CleaningWorker, AFailingHeartbeatSinkDoesNotFailTheFile) {
    // A transport failure is not "this input file is bad". The catch around
    // clean_fn_ used to swallow beat() transport exceptions along with store
    // exceptions; both became events = -1, which the coordinator explicitly
    // does not re-dispatch -- a transient socket error on a good day-file
    // permanently failed it, and stderr blamed the CSV. beat() is
    // fire-and-forget now: the send failure is logged and the file completes.
    struct ThrowingSink : mas::IMessageSink {
        int calls = 0;
        void send(const mas::Message&) override {
            ++calls;
            throw std::runtime_error("Operation cannot be accomplished in current state");
        }
    };
    mas::test::FakeSource work;
    work.queue.push_back(mas::encode(mas::WorkItem{"day1.csv"}));
    work.queue.push_back(mas::make_stop());
    mas::test::FakeSink results;
    ThrowingSink hb;
    FakeStore store;
    mas::CleaningWorker w(work, results, hb, store, "w1",
        [](const std::string&, mas::IEventStore&,
           const std::function<void()>& beat) -> long long {
            beat();   // mid-file heartbeat, as BeatingStore would fire it
            return 7;
        });

    EXPECT_EQ(w.run(), 1);
    EXPECT_GT(hb.calls, 0) << "the heartbeat path must have been exercised";
    ASSERT_EQ(results.sent.size(), 2u);   // CLAIM + RESULT, both delivered
    const auto r = mas::decode_result(results.sent[1]);
    ASSERT_TRUE(r.has_value());
    EXPECT_EQ(r->in_path, "day1.csv");
    EXPECT_EQ(r->events, 7) << "a lost beat must not become events=-1";
}

} // namespace
