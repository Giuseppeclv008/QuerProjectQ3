#include "mas/domain/Pipeline.hpp"
#include "mas/store/DuckDbEventStore.hpp"
#include <chrono>
#include <exception>
#include <iomanip>
#include <iostream>
#include <string>
#include <vector>

namespace {

double seconds_since(std::chrono::steady_clock::time_point t0) {
    return std::chrono::duration<double>(std::chrono::steady_clock::now() - t0)
        .count();
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
    if (threads > 1) {
        std::cerr << "error: threads > 1 lands in the next commit\n";
        return 2;
    }
    std::vector<std::string> files;
    for (int i = 4; i < argc; ++i) files.emplace_back(argv[i]);

    try {
        // Baseline arch "mono-1T" (spec §4): one process, one store, one file
        // after another — the exact hot path the MAS workers run, minus IPC.
        const auto t0 = std::chrono::steady_clock::now();
        mas::DuckDbEventStore store(out, machine);
        long long events = 0;
        for (const auto& f : files) {
            const long long n = mas::clean_file(f, store);
            if (n < 0) {
                std::cerr << "error: cannot clean " << f << "\n";
                return 1;
            }
            events += n;
        }
        const double clean_s = seconds_since(t0);
        const double merge_s = 0.0;   // sequential path writes one store: no merge
        std::cerr << "monolith: " << files.size() << " files, " << events
                  << " events, clean " << std::fixed << std::setprecision(3)
                  << clean_s << " s, merge " << merge_s << " s, total "
                  << (clean_s + merge_s) << " s, store holds " << store.count()
                  << " rows\n";
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << "\n";
        return 1;
    }
}
