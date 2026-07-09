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
