#include "mas/domain/Pipeline.hpp"
#include "mas/store/DuckDbEventStore.hpp"
#include "mas/store/ParquetEventStore.hpp"
#include "mas/store/EventStore.hpp"
#include "mas/util/engine.hpp"
#include "mas/util/platform_metrics.hpp"
#if MAS_CUDA_ENABLED
#include "CudaCleaner.hpp"
#endif
#include <atomic>
#include <chrono>
#include <filesystem>
#include <map>
#include <cstddef>
#include <cstdio>
#include <exception>
#include <iomanip>
#include <iostream>
#include <span>
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

// Counts events and discards them. Lets the benchmark time the clean path
// without DuckDB dominating it (spec §6.1): at ~15 ms of GPU work per day-file,
// the store write is two orders of magnitude larger and would flatten every
// arch into the same number.
class NullEventStore : public mas::IEventStore {
public:
    void write(std::span<const mas::CapEvent> events) override {
        n_ += static_cast<long long>(events.size());
    }
    long long count() const { return n_; }

private:
    long long n_ = 0;
};

// The GPU path exists in this binary only when CMake compiled it in
// (MAS_ENABLE_CUDA); the flag below mirrors that so resolve_engine can refuse
// --engine=cuda on a build that never had the kernels.
#if MAS_CUDA_ENABLED
constexpr bool kCudaCompiled = true;
#else
constexpr bool kCudaCompiled = false;
#endif

} // namespace

int main(int argc, char** argv) {
    mas::metrics_init();
    int argi = 1;
    bool no_store = false;
    bool parquet = false;
    mas::Engine engine = mas::Engine::Cpu;
    while (argi < argc && std::string(argv[argi]).rfind("--", 0) == 0) {
        const std::string arg = argv[argi];
        if (arg == "--no-store") {
            no_store = true;
        } else if (arg == "--format") {
            if (argi + 1 >= argc) {
                std::cerr << "error: --format needs a value\n";
                return 2;
            }
            const std::string fmt = argv[++argi];
            if (fmt == "parquet") parquet = true;
            else if (fmt != "duckdb") {
                std::cerr << "error: --format must be duckdb or parquet\n";
                return 2;
            }
        } else if (arg.rfind("--engine=", 0) == 0) {
            auto choice = mas::parse_engine(arg.substr(9));
            if (choice.ok) choice = mas::resolve_engine(choice.engine, kCudaCompiled);
            if (!choice.ok) {
                std::cerr << "error: " << choice.error << "\n";
                return 2;
            }
            engine = choice.engine;
        } else {
            std::cerr << "unknown flag " << arg << "\n";
            return 2;
        }
        ++argi;
    }
    if (argc - argi < 4) {
        std::cerr << "usage: mas_monolith [--no-store] [--engine=cpu|cuda] "
                     "[--format duckdb|parquet] <out.duckdb|out_dir> <machine_id> "
                     "<threads> <day1.csv> [day2.csv ...]\n";
        return 2;
    }
    // --format parquet is the benchmark's second backend, not a way to run the
    // system: it writes one Parquet per day-file and keeps no index, which is
    // what makes it fast to write and slow to read (docs/bench/results.md).
    if (no_store && parquet) {
        std::cerr << "error: --no-store and --format parquet contradict each "
                     "other (one persists nothing, the other persists Parquet)\n";
        return 2;
    }
    const std::string out = argv[argi++], machine = argv[argi++];
    int threads = 0;
    try {
        threads = std::stoi(argv[argi++]);
    } catch (const std::exception&) {
        std::cerr << "error: threads must be a number\n";
        return 2;
    }
    if (threads < 1) {
        std::cerr << "error: threads must be >= 1\n";
        return 2;
    }
    if (no_store && threads != 1) {
        std::cerr << "error: --no-store requires threads = 1 "
                     "(the MT path merges per-thread stores)\n";
        return 2;
    }
    if (engine == mas::Engine::Cuda && threads != 1) {
        std::cerr << "error: --engine=cuda requires threads = 1 "
                     "(the thread pool parallelizes CPU cleaning; the GPU "
                     "path is one device fed file by file)\n";
        return 2;
    }
    std::vector<std::string> files;
    for (int i = argi; i < argc; ++i) files.emplace_back(argv[i]);

    // The Parquet output is named after the input's basename, and that name IS
    // the idempotency mechanism -- so two inputs sharing one, from different
    // directories, silently collapse into a single output file. At threads > 1
    // they also race, two COPY ... TO writing the same path. Refused up front
    // rather than discovered as a short row count.
    if (parquet) {
        std::map<std::string, std::string> seen;
        for (const auto& f : files) {
            const auto stem = std::filesystem::path(f).stem().string();
            const auto [it, fresh] = seen.emplace(stem, f);
            if (!fresh) {
                std::cerr << "error: --format parquet names its output after the "
                             "input basename, and two inputs share '" << stem
                          << "':\n  " << it->second << "\n  " << f
                          << "\nthey would write the same file; rename one or "
                             "run them separately\n";
                return 2;
            }
        }
    }

    // Cleans one day-file into `store` with the selected engine and returns
    // the event count, or -1 after printing the error. The GPU branch refuses
    // to exist in a non-CUDA build rather than fall back: the summary line
    // below stamps the engine, and a stamp that could silently mean "cpu
    // actually" would make it worthless.
    const auto clean_into = [&](const std::string& f,
                                mas::IEventStore& store) -> long long {
#if MAS_CUDA_ENABLED
        if (engine == mas::Engine::Cuda) {
            std::vector<mas::CapEvent> events;
            mas::CudaStageTimes t;
            std::string error;
            if (!mas::cuda_clean_file(f, events, t, error)) {
                std::cerr << "error: " << error << "\n";
                return -1;
            }
            store.write(events);
            return static_cast<long long>(events.size());
        }
#endif
        return mas::clean_file(f, store);
    };

    try {
        const auto t0 = std::chrono::steady_clock::now();
        long long events = 0;
        double clean_s = 0.0, merge_s = 0.0;
        long long rows = 0;

        if (threads == 1) {
            if (no_store) {
                NullEventStore store;
                for (const auto& f : files) {
                    const long long n = clean_into(f, store);
                    if (n < 0) {
                        std::cerr << "error: cannot clean " << f << "\n";
                        return 1;
                    }
                    events += n;
                }
                clean_s = seconds_since(t0);
                // The store saw every write; its count must equal the sum of
                // clean_file's returns, or one of the two miscounts batches.
                if (store.count() != events) {
                    std::cerr << "error: clean_file returned " << events
                              << " events but the store received "
                              << store.count() << "\n";
                    return 1;
                }
                rows = 0;   // nothing persisted; "store holds 0 rows" is true
            } else if (parquet) {
                // A store per input file, not per run: IEventStore::write()
                // never learns that a file is finished, so a shared store
                // would have to buffer all 21.9M events before it could name
                // anything (spec §3.1).
                for (const auto& f : files) {
                    mas::ParquetEventStore store(mas::parquet_path_for(out, f), machine);
                    const long long n = clean_into(f, store);
                    if (n < 0) {
                        // Leave no file behind for a day that failed: the
                        // reader globs the directory and a valid empty Parquet
                        // is indistinguishable from a day with no events.
                        store.abandon();
                        std::cerr << "error: cannot clean " << f << "\n";
                        return 1;
                    }
                    store.close();
                    events += n;
                }
                clean_s = seconds_since(t0);
                rows = events;
            } else {
                // Baseline arch "mono-1T": one store, one file after another.
                mas::DuckDbEventStore store(out, machine);
                for (const auto& f : files) {
                    const long long n = clean_into(f, store);
                    if (n < 0) {
                        std::cerr << "error: cannot clean " << f << "\n";
                        return 1;
                    }
                    events += n;
                }
                clean_s = seconds_since(t0);
                rows = store.count();
            }
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
                    if (parquet) {
                        for (std::size_t i; (i = next.fetch_add(1)) < files.size();) {
                            mas::ParquetEventStore local(
                                mas::parquet_path_for(out, files[i]), machine);
                            per_file[i] = mas::clean_file(files[i], local);
                            if (per_file[i] < 0) {
                                local.abandon();   // see the 1T branch
                                failed = true;
                            } else {
                                local.close();
                            }
                        }
                        return;
                    }
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

            if (parquet) {
                merge_s = 0.0;
                for (const auto n : per_file) rows += n;
            } else {
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
                // Stop the clock BEFORE the cleanup. mas_merge does not delete its
                // sources — run_bench.sh clears them outside its timing window — so
                // charging mono-MT for ~1.5 GB of unlinks and leaving the MAS
                // untimed made merge_s measure two different things and biased the
                // architecture comparison toward the MAS by however long the
                // unlinks take.
                merge_s = seconds_since(tm);
                // The per-thread stores used to survive the run. Since the store
                // appends, a later run over a *different* file set re-merged the
                // previous run's rows into the new one, silently — build_store.sh
                // removed only the destination.
                for (int t = 0; t < threads; ++t) remove_store(thread_store(out, t));
            }
        }

        // The engine stamp rides the summary line so a pasted log can never
        // detach the numbers from the engine that produced them. Appended
        // last: run_bench.sh and run_bench_cuda.py match their fields by
        // substring, so a suffix is invisible to both.
        std::cerr << "monolith: " << files.size() << " files, " << events
                  << " events, clean " << std::fixed << std::setprecision(3)
                  << clean_s << " s, merge " << merge_s << " s, total "
                  << (clean_s + merge_s) << " s, store holds " << rows
                  << " rows, engine " << mas::engine_name(engine) << "\n";
        std::cerr << mas::metrics_line("clean", mas::read_metrics()) << "\n";
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << "\n";
        return 1;
    }
}
