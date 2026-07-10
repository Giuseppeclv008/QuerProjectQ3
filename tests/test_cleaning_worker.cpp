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
    mas::test::FakeSink results, hb;
    FakeStore store;
    std::vector<std::string> cleaned;
    mas::CleaningWorker w(work, results, hb, store, "w1",
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
    EXPECT_EQ(r0->worker_id, "w1");
    const auto r1 = mas::decode_result(results.sent[1]);
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
        [](const std::string&, mas::IEventStore&) -> long long { return 7; });
    EXPECT_EQ(w.run(), 1);
    ASSERT_EQ(results.sent.size(), 1u);
    const auto r = mas::decode_result(results.sent[0]);
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
        [](const std::string&, mas::IEventStore&) -> long long { return -1; });
    EXPECT_EQ(w.run(), 1);
    ASSERT_EQ(results.sent.size(), 1u);
    const auto r = mas::decode_result(results.sent[0]);
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
    FakeStore store;
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
    FakeStore store;
    mas::CleaningWorker worker(work, results, hb, store, "w1",
        [](const std::string&, mas::IEventStore&) { return 1LL; });

    // 2*(budget-1) empty ticks straddle one item: never 60 consecutive,
    // so the worker survives to the STOP and handles the item.
    EXPECT_EQ(worker.run(), 1);
    ASSERT_EQ(results.sent.size(), 1u);
}

} // namespace
