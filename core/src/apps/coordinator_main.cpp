#include "mas/agent/Coordinator.hpp"
#include "mas/transport/ZmqTransport.hpp"
#include <chrono>
#include <exception>
#include <iostream>
#include <string>
#include <string_view>
#include <vector>

int main(int argc, char** argv) {
    const std::string usage =
        "usage: mas_coordinator <work_endpoint> <result_endpoint> <hb_endpoint> "
        "[--workers N] <day1.csv> [day2.csv ...]\n";
    if (argc < 5) {
        std::cerr << usage;
        return 2;
    }
    try {
        const std::string work_ep = argv[1], result_ep = argv[2], hb_ep = argv[3];

        // Optional "--workers N" immediately after <hb_endpoint>: gates the
        // initial dispatch on N workers registering (Plan 5 fix for the PUSH
        // slow-joiner capture). Absent -> expected_workers stays 0, the old
        // dispatch-immediately behavior existing callers depend on.
        int next = 4;
        int expected_workers = 0;
        if (next < argc && std::string(argv[next]) == "--workers") {
            if (next + 1 >= argc) {
                std::cerr << usage;
                return 2;
            }
            const std::string n_str = argv[next + 1];
            bool valid = true;
            try {
                std::size_t end = 0;
                expected_workers = std::stoi(n_str, &end);
                // Wire/CLI numeric fields are untrusted: require the whole
                // token to be consumed (matches Message.cpp's decode style),
                // so trailing garbage like "4x" is rejected, not truncated.
                if (end != n_str.size()) valid = false;
            } catch (const std::exception&) {
                valid = false;
            }
            if (!valid || expected_workers < 1) {
                std::cerr << usage;
                return 2;
            }
            next += 2;
        }
        if (next >= argc) {
            std::cerr << usage;
            return 2;
        }

        std::vector<mas::WorkItem> items;
        for (int i = next; i < argc; ++i) {
            // The wire format is newline-delimited with no escaping
            // (Message.hpp): a path carrying '\n' encodes to a frame every
            // decoder rejects, the worker drops it silently, and the item
            // hangs open until the death machinery times the run out. Refuse
            // it here, where the operator can still read the answer.
            if (std::string_view(argv[i]).find('\n') != std::string_view::npos) {
                std::cerr << "error: day-file path contains a newline, which "
                             "the newline-delimited wire format cannot carry: "
                          << argv[i] << "\n";
                return 2;
            }
            items.push_back({argv[i]});
        }

        zmq::context_t ctx(1);
        // Send-side liveness: a mute work socket (nobody ever drains it)
        // throws after 60 s instead of hanging this process forever.
        // linger_ms bounded at 2 s: without it the sentinel couples linger to
        // the 60 s send timeout, and teardown with any undelivered frame
        // (e.g. a STOP for a peer that just vanished) stalled the process the
        // full minute -- the same class of bug fixed on the worker's sinks.
        // Not 0 like the worker's: this socket carries the STOPs, and 2 s is
        // ample to flush them to every connected live pipe on loopback.
        mas::ZmqPushSink work(ctx, work_ep, /*bind=*/true,
                              /*send_timeout_ms=*/60000, /*linger_ms=*/2000);
        // 200 ms of results silence = one loop tick: paces heartbeat drains
        // and deadline sweeps (resilience spec §6).
        mas::ZmqPullSource results(ctx, result_ep, /*bind=*/true,
                                   /*timeout_ms=*/200);
        // Heartbeats are drained without blocking each tick.
        mas::ZmqPullSource heartbeats(ctx, hb_ep, /*bind=*/true,
                                      /*timeout_ms=*/0);
        mas::CoordinatorConfig cfg{};
        cfg.expected_workers = expected_workers;
        const auto s = mas::run_coordinator(
            items, work, results, heartbeats, cfg,
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
