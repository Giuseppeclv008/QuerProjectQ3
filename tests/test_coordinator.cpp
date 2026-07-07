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
