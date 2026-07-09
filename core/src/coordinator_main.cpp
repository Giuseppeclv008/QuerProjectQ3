#include "mas/Coordinator.hpp"
#include "mas/ZmqTransport.hpp"
#include <chrono>
#include <exception>
#include <iostream>
#include <string>
#include <vector>

int main(int argc, char** argv) {
    if (argc < 4) {
        std::cerr << "usage: mas_coordinator <work_endpoint> <result_endpoint> <day1.csv> [day2.csv ...]\n";
        return 2;
    }
    try {
        const std::string work_ep = argv[1], result_ep = argv[2];
        std::vector<mas::WorkItem> items;
        for (int i = 3; i < argc; ++i) items.push_back({argv[i]});

        zmq::context_t ctx(1);
        // Send-side liveness: send_timeout_ms=60000 sets ZMQ_SNDTIMEO and a
        // finite ZMQ_LINGER, so a mute work socket (no workers ever connect,
        // or all workers die mid-run) throws after 60 s instead of hanging
        // this process forever. Start workers first for prompt dispatch;
        // richer recovery (heartbeats, re-dispatch) is the resilience plan's.
        mas::ZmqPushSink work(ctx, work_ep, /*bind=*/true, /*send_timeout_ms=*/60000);
        // 60 s of sink silence => count stragglers as failed instead of
        // hanging forever (heartbeat-driven re-dispatch is a later plan).
        mas::ZmqPullSource results(ctx, result_ep, /*bind=*/true,
                                   /*timeout_ms=*/60000);
        mas::ZmqPullSource heartbeats(ctx, "tcp://127.0.0.1:5559", /*bind=*/true,
                                      /*timeout_ms=*/0);
        const auto s = mas::run_coordinator(items, work, results, heartbeats,
                                            mas::CoordinatorConfig{},
                                            [] { return std::chrono::steady_clock::now(); });
        std::cerr << "dispatched " << items.size() << " files: " << s.files_ok
                  << " ok, " << s.files_failed << " failed, "
                  << s.total_events << " events\n";
        return s.files_failed == 0 ? 0 : 1;
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << "\n";
        return 1;
    }
}
