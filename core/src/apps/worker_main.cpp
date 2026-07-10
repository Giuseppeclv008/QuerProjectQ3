#include "mas/agent/CleaningWorker.hpp"
#include "mas/store/DuckDbEventStore.hpp"
#include "mas/domain/Pipeline.hpp"
#include "mas/transport/ZmqTransport.hpp"
#include <exception>
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    if (argc < 6) {
        std::cerr << "usage: mas_worker <work_endpoint> <result_endpoint> "
                     "<hb_endpoint> <out.duckdb> <worker_id> [machine_id]\n";
        return 2;
    }
    const std::string work_ep = argv[1], result_ep = argv[2], hb_ep = argv[3];
    const std::string out = argv[4], worker_id = argv[5];
    const std::string machine = (argc > 6) ? argv[6] : "MCC";
    try {
        zmq::context_t ctx(1);
        // Liveness (resilience spec §7): the 1 s work recv timeout is the
        // wait tick — each empty tick heartbeats, 60 in a row exits. Send
        // timeouts turn a dead coordinator into a thrown error instead of a
        // forever-blocked PUSH.
        mas::ZmqPullSource work(ctx, work_ep, /*bind=*/false,
                                /*timeout_ms=*/1000);
        // linger_ms=0 on both sinks so process exit is prompt when the
        // coordinator is gone (chaos E2E: the coupled 60 s linger held the
        // orphan worker to 121 s vs the ~65 s budget). Protocol-safe:
        // results — the coordinator STOPs only after every item is settled,
        // so a result still queued at exit means the coordinator is dead and
        // the item gets re-dispatched from a survivor anyway; heartbeats —
        // fire-and-forget liveness, worthless once this process ends.
        mas::ZmqPushSink results(ctx, result_ep, /*bind=*/false,
                                 /*send_timeout_ms=*/60000, /*linger_ms=*/0);
        mas::ZmqPushSink heartbeats(ctx, hb_ep, /*bind=*/false,
                                    /*send_timeout_ms=*/60000, /*linger_ms=*/0);
        mas::DuckDbEventStore store(out, machine);
        mas::CleaningWorker worker(work, results, heartbeats, store, worker_id,
            [](const std::string& path, mas::IEventStore& s) {
                return mas::clean_file(path, s);
            });
        const int handled = worker.run();
        std::cerr << "worker " << worker_id << " done: " << handled
                  << " work items, store holds " << store.count() << " rows\n";
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << "\n";
        return 1;
    }
    return 0;
}
