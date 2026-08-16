#include "mas/agent/CleaningWorker.hpp"
#include "mas/agent/Coordinator.hpp"
#include "mas/domain/Pipeline.hpp"
#include "mas/transport/ZmqTransport.hpp"
#include "fakes/TempPath.hpp"
#include <gtest/gtest.h>
#include <cstdio>
#include <fstream>
#include <span>
#include <string>
#include <thread>
#include <vector>

namespace {

// The missing rung between the unit tests (every coordinator and worker test
// runs on FakeTransport) and a chaos script that cannot run in CI: one
// coordinator plus one worker over inproc://, real sockets, real codec, real
// clean_file, a real 3-row day-file. Runs in well under a second.

// In-memory store: the axis under test is the transport, not persistence.
struct MemStore : mas::IEventStore {
    std::vector<mas::CapEvent> events;
    void write(std::span<const mas::CapEvent> batch) override {
        events.insert(events.end(), batch.begin(), batch.end());
    }
};

std::string writeTinyDay(const std::string& path) {
    std::ofstream f(path, std::ios::binary);
    f << "timestamp";
    auto col = [&](const char* name) {
        for (int i = 1; i <= mas::NUM_HEADS; ++i)
            f << ",H" << (i < 10 ? "0" : "") << i << " " << name;
    };
    col("Count");
    col("AppTorque");
    col("Status");
    f << "\n";
    // Row 0 seeds; rows 1-2 advance head 1 by one cap each: 2 events.
    for (int r = 0; r < 3; ++r) {
        f << "2026-02-01T00:00:0" << r << ".000";
        for (int h = 0; h < mas::NUM_HEADS; ++h) f << "," << (h == 0 ? 100 + r : 50);
        for (int h = 0; h < mas::NUM_HEADS; ++h) f << ",2.5";
        for (int h = 0; h < mas::NUM_HEADS; ++h) f << ",0";
        f << "\n";
    }
    return path;
}

TEST(ZmqE2E, CoordinatorAndWorkerSettleARealFileOverInproc) {
    const std::string day = writeTinyDay(mas::test::temp_artifact("t_zmq_e2e_day.csv"));

    zmq::context_t ctx(1);
    // inproc requires bind before connect: coordinator sockets first.
    mas::ZmqPushSink work(ctx, "inproc://e2e-work", /*bind=*/true,
                          /*send_timeout_ms=*/5000);
    mas::ZmqPullSource results(ctx, "inproc://e2e-results", /*bind=*/true,
                               /*timeout_ms=*/200);
    mas::ZmqPullSource heartbeats(ctx, "inproc://e2e-hb", /*bind=*/true,
                                  /*timeout_ms=*/0);

    MemStore store;
    int handled = -1;
    std::thread worker_thread([&] {
        mas::ZmqPullSource w_work(ctx, "inproc://e2e-work", /*bind=*/false,
                                  /*timeout_ms=*/200);
        mas::ZmqPushSink w_results(ctx, "inproc://e2e-results", /*bind=*/false,
                                   /*send_timeout_ms=*/5000);
        mas::ZmqPushSink w_hb(ctx, "inproc://e2e-hb", /*bind=*/false,
                              /*send_timeout_ms=*/5000);
        mas::CleaningWorker worker(
            w_work, w_results, w_hb, store, "w1",
            [](const std::string& path, mas::IEventStore& s,
               const std::function<void()>&) {
                return mas::clean_file(path, s);
            });
        handled = worker.run();
    });

    const std::vector<mas::WorkItem> items = {{day}};
    mas::CoordinatorConfig cfg;
    cfg.expected_workers = 1;   // the registration gate, end to end
    const auto s = mas::run_coordinator(items, work, results, heartbeats, cfg,
                                        [] { return std::chrono::steady_clock::now(); });
    worker_thread.join();

    EXPECT_EQ(s.files_ok, 1);
    EXPECT_EQ(s.files_failed, 0);
    EXPECT_EQ(s.workers_died, 0);
    EXPECT_EQ(s.total_events, 2);
    EXPECT_EQ(handled, 1);
    EXPECT_EQ(store.events.size(), 2u);
    std::remove(day.c_str());
}

TEST(ZmqE2E, WorkerFailureRouteSurvivesTheRealTransport) {
    // The events=-1 channel over real sockets: an unreadable input is a
    // failed item and a healthy worker, exactly as with the fakes.
    zmq::context_t ctx(1);
    mas::ZmqPushSink work(ctx, "inproc://e2e2-work", true, 5000);
    mas::ZmqPullSource results(ctx, "inproc://e2e2-results", true, 200);
    mas::ZmqPullSource heartbeats(ctx, "inproc://e2e2-hb", true, 0);

    MemStore store;
    std::thread worker_thread([&] {
        mas::ZmqPullSource w_work(ctx, "inproc://e2e2-work", false, 200);
        mas::ZmqPushSink w_results(ctx, "inproc://e2e2-results", false, 5000);
        mas::ZmqPushSink w_hb(ctx, "inproc://e2e2-hb", false, 5000);
        mas::CleaningWorker worker(
            w_work, w_results, w_hb, store, "w1",
            [](const std::string& path, mas::IEventStore& s,
               const std::function<void()>&) {
                return mas::clean_file(path, s);
            });
        worker.run();
    });

    const std::vector<mas::WorkItem> items = {{mas::test::temp_artifact("definitely_not_here.csv")}};
    mas::CoordinatorConfig cfg;
    cfg.expected_workers = 1;
    const auto s = mas::run_coordinator(items, work, results, heartbeats, cfg,
                                        [] { return std::chrono::steady_clock::now(); });
    worker_thread.join();

    EXPECT_EQ(s.files_ok, 0);
    EXPECT_EQ(s.files_failed, 1);
    EXPECT_EQ(s.workers_died, 0) << "a failed item must not read as a dead worker";
}

} // namespace
