#include "mas/agent/CleaningWorker.hpp"
#include "mas/store/BeatingStore.hpp"
#include "mas/store/DuckDbEventStore.hpp"
#include "mas/store/ParquetEventStore.hpp"
#include "mas/domain/Pipeline.hpp"
#include "mas/transport/ZmqTransport.hpp"
#include <exception>
#include <iostream>
#include <optional>
#include <span>
#include <string>

namespace {

// In parquet mode the store CleaningWorker is constructed with is never
// written to: clean_fn builds and closes its own ParquetEventStore per work
// item (Task 3 spec §3.1) and ignores the store handed to it. This exists
// only so CleaningWorker always has a real IEventStore& to wrap in its own
// BeatingStore, without opening a DuckDB file at what is, in parquet mode, a
// directory path.
struct NullStore : mas::IEventStore {
    void write(std::span<const mas::CapEvent>) override {}
};

} // namespace

int main(int argc, char** argv) {
    if (argc < 6) {
        std::cerr << "usage: mas_worker [--format duckdb|parquet] <work_endpoint> "
                     "<result_endpoint> <hb_endpoint> <out.duckdb|out_dir> "
                     "<worker_id> [machine_id]\n";
        return 2;
    }
    int argi = 1;
    bool parquet = false;
    if (std::string(argv[argi]) == "--format") {
        if (argc < argi + 2) { std::cerr << "error: --format needs a value\n"; return 2; }
        const std::string fmt = argv[argi + 1];
        if (fmt == "parquet") parquet = true;
        else if (fmt != "duckdb") { std::cerr << "error: --format must be duckdb or parquet\n"; return 2; }
        argi += 2;
    }
    if (argc - argi < 5) {
        std::cerr << "usage: mas_worker [--format duckdb|parquet] <work_endpoint> "
                     "<result_endpoint> <hb_endpoint> <out.duckdb|out_dir> "
                     "<worker_id> [machine_id]\n";
        return 2;
    }
    const std::string work_ep = argv[argi], result_ep = argv[argi + 1],
                       hb_ep = argv[argi + 2];
    const std::string out = argv[argi + 3], worker_id = argv[argi + 4];
    const std::string machine = (argc > argi + 5) ? argv[argi + 5] : "MCC";
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
        std::optional<mas::DuckDbEventStore> duck;
        NullStore null_store;
        if (!parquet) duck.emplace(out, machine);
        mas::IEventStore& store = parquet
            ? static_cast<mas::IEventStore&>(null_store)
            : static_cast<mas::IEventStore&>(*duck);
        mas::CleaningWorker worker(work, results, heartbeats, store, worker_id,
            [&](const std::string& path, mas::IEventStore& s,
                const std::function<void()>& beat) {
                if (!parquet) return mas::clean_file(path, s);
                mas::ParquetEventStore pq(mas::parquet_path_for(out, path), machine);
                // Same decorator the worker applies to its injected store, so
                // the parquet path is not silent for the length of a file.
                mas::BeatingStore beating(pq, beat, mas::CleaningWorker::kBeatEvery);
                const long long n = mas::clean_file(path, beating);
                pq.close();
                return n;
            });
        const int handled = worker.run();
        if (parquet) {
            std::cerr << "worker " << worker_id << " done: " << handled
                      << " work items\n";
        } else {
            std::cerr << "worker " << worker_id << " done: " << handled
                      << " work items, store holds " << duck->count() << " rows\n";
        }
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << "\n";
        return 1;
    }
    return 0;
}
