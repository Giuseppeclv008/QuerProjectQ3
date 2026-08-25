#include "mas/agent/CleaningWorker.hpp"
#include "mas/apps/CliArgs.hpp"
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
// item (parquet-store spec §3.1) and ignores the store handed to it. This exists
// only so CleaningWorker always has a real IEventStore& to wrap in its own
// BeatingStore, without opening a DuckDB file at what is, in parquet mode, a
// directory path.
struct NullStore : mas::IEventStore {
    void write(std::span<const mas::CapEvent>) override {}
};

} // namespace

int main(int argc, char** argv) {
    // 2, not 6: arity is checked after flag parsing, below. The old guard made
    // "--format needs a value" unreachable.
    if (argc < 2) {
        std::cerr << "usage: mas_worker [--format duckdb|parquet] <work_endpoint> "
                     "<result_endpoint> <hb_endpoint> <out.duckdb|out_dir> "
                     "<worker_id> <machine_id>\n";
        return 2;
    }
    int argi = 1;
    bool parquet = false;
    if (std::string(argv[argi]) == "--format") {
        if (argi + 1 >= argc) { std::cerr << "error: --format needs a value\n"; return 2; }
        const std::string fmt = argv[argi + 1];
        if (fmt == "parquet") parquet = true;
        else if (fmt != "duckdb") { std::cerr << "error: --format must be duckdb or parquet\n"; return 2; }
        argi += 2;
    }
    if (const auto bad = mas::unconsumed_flag(argc, argv, argi)) {
        std::cerr << "error: " << *bad << "\n";
        return 2;
    }
    if (argc - argi != 6) {
        // machine_id is required, not defaulted: CliArgs.hpp states the
        // governing principle -- a wrong answer that announces itself as a
        // success -- and a store silently labelled "MCC" is exactly that. A
        // merge into a destination labelled with the real id then holds rows
        // no machine-scoped analytics query will ever find.
        std::cerr << "usage: mas_worker [--format duckdb|parquet] <work_endpoint> "
                     "<result_endpoint> <hb_endpoint> <out.duckdb|out_dir> "
                     "<worker_id> <machine_id>\n";
        return 2;
    }
    const std::string work_ep = argv[argi], result_ep = argv[argi + 1],
                       hb_ep = argv[argi + 2];
    const std::string out = argv[argi + 3], worker_id = argv[argi + 4];
    if (worker_id.find('\n') != std::string::npos) {
        std::cerr << "error: worker_id contains a newline, which the "
                     "newline-delimited wire format cannot carry\n";
        return 2;
    }
    const std::string machine = argv[argi + 5];
    try {
        zmq::context_t ctx(1);
        // Liveness (resilience spec §7): the 1 s work recv timeout is the
        // wait tick — each empty tick heartbeats, 60 in a row exits. Send
        // timeouts turn a dead coordinator into a thrown error instead of a
        // forever-blocked PUSH.
        mas::ZmqPullSource work(ctx, work_ep, /*bind=*/false,
                                /*timeout_ms=*/1000);
        // Heartbeats keep linger_ms=0: fire-and-forget liveness, worthless
        // once this process ends, and a zero linger is what keeps an orphan
        // worker's exit prompt when the coordinator is gone (chaos E2E: the
        // coupled 60 s linger held the orphan to 121 s vs the ~65 s budget).
        //
        // The results sink gets a short REAL linger. The zero-linger rationale
        // ("a result still queued at exit means the coordinator is dead")
        // predates the Goodbye frame, which is sent precisely when the
        // coordinator is ALIVE -- at idle exit, immediately before the socket
        // closes. With linger 0 that BYE can be silently dropped, and losing
        // it reverts the departure to death semantics: completions reopened,
        // items re-dispatched, workers_died inflated. 300 ms is enough for a
        // localhost flush and bounds the orphan case at well under the chaos
        // budget.
        mas::ZmqPushSink results(ctx, result_ep, /*bind=*/false,
                                 /*send_timeout_ms=*/60000,
                                 /*linger_ms=*/mas::kResultSinkLingerMs);
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
                // skipped-row counts go to stderr per file: a malformed row is
                // data loss the RESULT frame does not carry, and only the
                // worker's log can say it happened.
                mas::CleanFileStats stats;
                const auto warn = [&] {
                    if (stats.skipped_rows)
                        std::cerr << "worker " << worker_id << ": " << path
                                  << ": skipped " << stats.skipped_rows
                                  << " malformed rows\n";
                    if (stats.out_of_order_rows)
                        std::cerr << "worker " << worker_id << ": " << path
                                  << ": " << stats.out_of_order_rows
                                  << " out-of-order timestamps\n";
                };
                if (!parquet) {
                    const long long n = mas::clean_file(path, s, &stats);
                    warn();
                    return n;
                }
                mas::ParquetEventStore pq(mas::parquet_path_for(out, path), machine);
                // Same decorator the worker applies to its injected store, so
                // the parquet path is not silent for the length of a file.
                mas::BeatingStore beating(pq, beat, mas::CleaningWorker::kBeatEvery);
                const long long n = mas::clean_file(path, beating, &stats);
                warn();
                if (n < 0) {
                    pq.abandon();   // no file for a work item that failed
                    return n;
                }
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
