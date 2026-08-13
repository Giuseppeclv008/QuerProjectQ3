#include "mas/store/CsvRawReader.hpp"
#include "mas/store/DuckDbEventStore.hpp"
#include "mas/store/ParquetEventStore.hpp"
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
        std::cerr << "usage: clean [--format duckdb|parquet] <raw_in.csv> "
                     "<events_out.csv|.duckdb|out_dir> [machine_id]\n";
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
    if (argc - argi < 2) {
        std::cerr << "usage: clean [--format duckdb|parquet] <raw_in.csv> "
                     "<events_out.csv|.duckdb|out_dir> [machine_id]\n";
        return 2;
    }
    const std::string in = argv[argi], out = argv[argi + 1];
    const std::string machine = (argc > argi + 2) ? argv[argi + 2] : "MCC";

    if (parquet) {
        mas::CsvRawReader probe(in);
        if (!probe.is_open()) return report_missing_input(in);
        try {
            mas::ParquetEventStore store(mas::parquet_path_for(out, in), machine);
            const long long n = mas::clean_file(in, store);
            store.close();
            std::cerr << "wrote " << n << " cap events to parquet\n";
        } catch (const std::exception& e) {
            std::cerr << "error: " << e.what() << "\n";
            return 1;
        }
        return 0;
    }

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
