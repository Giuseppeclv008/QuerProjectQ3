// Store-free C++ contender for the CUDA benchmark (spec §5.4). Mirrors
// monolith_main.cpp's file-grain threading -- a fixed pool of T threads pulling
// file indices off an atomic counter -- but accumulates events in memory instead
// of writing DuckDB. Same CsvRawReader -> CapEventExtractor hot path.
//
// It exists so the headline measurement does not require DuckDB on the target
// machine. tests/test_bench_cpu_parity.cpp keeps the substitution honest.
#include "mas/domain/CapEventExtractor.hpp"
#include "mas/store/CsvRawReader.hpp"
#include "mas/util/platform_metrics.hpp"
#include <atomic>
#include <chrono>
#include <cstddef>
#include <exception>
#include <iomanip>
#include <iostream>
#include <string>
#include <thread>
#include <vector>

namespace {

long long cleanOne(const std::string& path) {
    mas::CsvRawReader reader(path);
    if (!reader.is_open()) return -1;
    mas::CapEventExtractor ex;
    mas::RawRow row;
    std::vector<mas::CapEvent> batch;
    long long n = 0;
    constexpr std::size_t kBatch = 8192;   // matches Pipeline::clean_file
    while (reader.next(row)) {
        ex.process(row, batch);
        if (batch.size() >= kBatch) { n += static_cast<long long>(batch.size()); batch.clear(); }
    }
    n += static_cast<long long>(batch.size());
    return n;
}

} // namespace

int main(int argc, char** argv) {
    mas::metrics_init();
    if (argc < 3) {
        std::cerr << "usage: bench_cpu <threads> <day1.csv> [day2.csv ...]\n";
        return 2;
    }
    int threads = 0;
    try { threads = std::stoi(argv[1]); }
    catch (const std::exception&) { std::cerr << "error: threads must be a number\n"; return 2; }
    if (threads < 1) { std::cerr << "error: threads must be >= 1\n"; return 2; }

    std::vector<std::string> files;
    for (int i = 2; i < argc; ++i) files.emplace_back(argv[i]);

    const auto t0 = std::chrono::steady_clock::now();
    std::vector<long long> per_file(files.size(), 0);
    std::atomic<std::size_t> next{0};

    auto pull = [&] {
        for (std::size_t i; (i = next.fetch_add(1)) < files.size();)
            per_file[i] = cleanOne(files[i]);
    };
    {
        std::vector<std::thread> pool;
        pool.reserve(static_cast<std::size_t>(threads));
        for (int t = 0; t < threads; ++t) pool.emplace_back(pull);
        for (auto& th : pool) th.join();
    }
    const double clean_s =
        std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();

    long long events = 0;
    for (std::size_t i = 0; i < files.size(); ++i) {
        if (per_file[i] < 0) { std::cerr << "error: cannot clean " << files[i] << "\n"; return 1; }
        events += per_file[i];
    }

    std::cerr << "bench_cpu: " << files.size() << " files, " << events
              << " events, clean " << std::fixed << std::setprecision(3)
              << clean_s << " s\n";
    std::cerr << mas::metrics_line("clean", mas::read_metrics()) << "\n";
    return 0;
}
