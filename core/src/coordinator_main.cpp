#include "mas/Coordinator.hpp"
#include "mas/ZmqTransport.hpp"
#include <exception>
#include <iostream>
#include <string>
#include <vector>

int main(int argc, char** argv) {
    if (argc < 5) {
        std::cerr << "usage: mas_coordinator <work_endpoint> <result_endpoint> <num_workers> <day1.csv> [day2.csv ...]\n";
        return 2;
    }
    try {
        const std::string work_ep = argv[1], result_ep = argv[2];
        const int num_workers = std::stoi(argv[3]);
        if (num_workers < 1) {
            std::cerr << "error: num_workers must be >= 1\n";
            return 2;
        }
        std::vector<mas::WorkItem> items;
        for (int i = 4; i < argc; ++i) items.push_back({argv[i]});

        zmq::context_t ctx(1);
        // Known limitation (deferred to the resilience plan): PUSH blocks forever
        // when no worker is connected (mute socket), and default infinite LINGER
        // can block teardown if a STOP goes undelivered. Start workers BEFORE the
        // coordinator; if all workers die mid-run, this process can hang rather
        // than time out — the 60 s results timeout below only covers lost work
        // items while at least one worker remains connected.
        mas::ZmqPushSink work(ctx, work_ep, /*bind=*/true, /*send_timeout_ms=*/60000);
        // 60 s of sink silence => count stragglers as failed instead of
        // hanging forever (heartbeat-driven re-dispatch is a later plan).
        // Symmetric on the send side: 60 s of send blockage (e.g. no workers
        // ever connect) throws instead of hanging the coordinator forever.
        mas::ZmqPullSource results(ctx, result_ep, /*bind=*/true,
                                   /*timeout_ms=*/60000);
        const auto s = mas::run_coordinator(items, work, results, num_workers);
        std::cerr << "dispatched " << items.size() << " files: " << s.files_ok
                  << " ok, " << s.files_failed << " failed, "
                  << s.total_events << " events\n";
        return s.files_failed == 0 ? 0 : 1;
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << "\n";
        return 1;
    }
}
