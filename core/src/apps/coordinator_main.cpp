#include "mas/agent/Coordinator.hpp"
#include "mas/transport/ZmqTransport.hpp"
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
