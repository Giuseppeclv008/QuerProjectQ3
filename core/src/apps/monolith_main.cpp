#include "mas/domain/Pipeline.hpp"
#include "mas/store/DuckDbEventStore.hpp"
#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdio>
#include <exception>
#include <iomanip>
#include <iostream>
#include <string>
#include <thread>
#include <vector>

namespace {

double seconds_since(std::chrono::steady_clock::time_point t0) {
    return std::chrono::duration<double>(std::chrono::steady_clock::now() - t0)
        .count();
}

std::string thread_store(const std::string& out, int t) {
    return out + ".t" + std::to_string(t) + ".duckdb";
}

// DuckDB leaves a .wal beside the database; removing only the .duckdb would let
// a stale write-ahead log be replayed into the next run's file of the same name.
void remove_store(const std::string& path) {
    std::remove(path.c_str());
    std::remove((path + ".wal").c_str());
}

} // namespace

int main(int argc, char** argv) {
    if (argc < 5) {
        std::cerr << "usage: mas_monolith <out.duckdb> <machine_id> <threads> "
                     "<day1.csv> [day2.csv ...]\n";
        return 2;
    }
    const std::string out = argv[1], machine = argv[2];
    int threads = 0;
    try {
        threads = std::stoi(argv[3]);
    } catch (const std::exception&) {
        std::cerr << "error: threads must be a number\n";
        return 2;
    }
    if (threads < 1) {
        std::cerr << "error: threads must be >= 1\n";
        return 2;
    }
    std::vector<std::string> files;
    for (int i = 4; i < argc; ++i) files.emplace_back(argv[i]);

    try {
        const auto t0 = std::chrono::steady_clock::now();
        long long events = 0;
        double clean_s = 0.0, merge_s = 0.0;
        long long rows = 0;

        if (threads == 1) {
            // Baseline arch "mono-1T": one store, one file after another.
            mas::DuckDbEventStore store(out, machine);
            for (const auto& f : files) {
                const long long n = mas::clean_file(f, store);
                if (n < 0) {
                    std::cerr << "error: cannot clean " << f << "\n";
                    return 1;
                }
                events += n;
            }
            clean_s = seconds_since(t0);
            rows = store.count();
        } else {
            // Arch "mono-MT" (spec §3): fixed pool of T threads pulling file
            // indices off an atomic counter — the same file-grain unit of
            // work as the MAS, threads instead of processes. Shared-nothing:
            // each clean_file call builds its own reader/extractor, and each
            // thread owns one store file, merged after the join (DuckDB is
            // single-writer; same strategy as the MAS sink).
            std::vector<long long> per_file(files.size(), 0);
            std::atomic<std::size_t> next{0};
            std::atomic<bool> failed{false};
            // A previous run's per-thread stores are removed before the pool
            // opens: DuckDbEventStore appends, so reusing one would fold an
            // earlier run's events into this one.
            for (int t = 0; t < threads; ++t) remove_store(thread_store(out, t));
            auto pull = [&](int t) {
                try {
                    mas::DuckDbEventStore local(thread_store(out, t), machine);
                    for (std::size_t i;
                         (i = next.fetch_add(1)) < files.size();) {
                        per_file[i] = mas::clean_file(files[i], local);
                        if (per_file[i] < 0) failed = true;
                    }
                } catch (const std::exception& e) {
                    std::cerr << "error: thread " << t << ": " << e.what()
                              << "\n";
                    failed = true;
                }
            };
            {
                std::vector<std::thread> pool;
                pool.reserve(static_cast<std::size_t>(threads));
                for (int t = 0; t < threads; ++t) pool.emplace_back(pull, t);
                for (auto& th : pool) th.join();
            }
            clean_s = seconds_since(t0);
            if (failed) {
                for (std::size_t i = 0; i < files.size(); ++i)
                    if (per_file[i] < 0)
                        std::cerr << "error: cannot clean " << files[i] << "\n";
                return 1;
            }
            for (const auto n : per_file) events += n;

            // Thread stores are closed (destroyed) here — merge_from's
            // closed/checkpointed precondition holds.
            const auto tm = std::chrono::steady_clock::now();
            {
                mas::DuckDbEventStore store(out, machine);
                std::vector<std::string> sources;
                sources.reserve(static_cast<std::size_t>(threads));
                for (int t = 0; t < threads; ++t)
                    sources.push_back(thread_store(out, t));
                store.merge_all(sources);
                rows = store.count();
            }   // close the destination before deleting its sources
            // The per-thread stores used to survive the run. Since the store
            // appends, a later run over a *different* file set re-merged the
            // previous run's rows into the new one, silently — build_store.sh
            // removed only the destination.
            for (int t = 0; t < threads; ++t) remove_store(thread_store(out, t));
            merge_s = seconds_since(tm);
        }

        std::cerr << "monolith: " << files.size() << " files, " << events
                  << " events, clean " << std::fixed << std::setprecision(3)
                  << clean_s << " s, merge " << merge_s << " s, total "
                  << (clean_s + merge_s) << " s, store holds " << rows
                  << " rows\n";
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << "\n";
        return 1;
    }
}
