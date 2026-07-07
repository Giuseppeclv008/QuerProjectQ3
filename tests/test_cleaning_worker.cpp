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
