#include "mas/store/CsvRawReader.hpp"
#include "mas/store/DuckDbEventStore.hpp"
#include "mas/domain/Pipeline.hpp"
#include <iostream>
#include <string>
#include <string_view>

namespace {

// Shared by both output modes: identical wording for a missing/unreadable input.
int report_missing_input(const std::string& in_path) {
    std::cerr << "error: cannot open input file " << in_path << "\n";
    return 1;
}

} // namespace

int main(int argc, char** argv) {
    if (argc < 3) {
        std::cerr << "usage: clean <raw_in.csv> <events_out.csv|events_out.duckdb> [machine_id]\n";
        return 2;
    }
    const std::string in = argv[1], out = argv[2];
    const std::string machine = (argc > 3) ? argv[3] : "MCC";

    if (std::string_view(out).ends_with(".duckdb")) {
        // Probe before constructing the store: DuckDbEventStore's constructor
        // creates the .duckdb file as a side effect, so a missing input must
        // never leave an empty database behind (mirrors the CSV wrapper's
        // probe in Pipeline.cpp).
        mas::CsvRawReader probe(in);
        if (!probe.is_open()) return report_missing_input(in);

        try {
            mas::DuckDbEventStore store(out, machine);
            const long long n = mas::clean_file(in, store);
            std::cerr << "wrote " << n << " cap events; store now holds "
                      << store.count() << " rows\n";
        } catch (const std::exception& e) {
            std::cerr << "error: " << e.what() << "\n";
            return 1;
        }
        return 0;
    }

    const long long n = mas::clean_file(in, out, machine);
    if (n == -1) return report_missing_input(in);
    if (n < 0) {
        std::cerr << "error: cannot write output file " << out << "\n";
        return 1;
    }
    std::cerr << "wrote " << n << " cap events\n";
    return 0;
}
