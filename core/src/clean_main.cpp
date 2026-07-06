#include "mas/DuckDbEventStore.hpp"
#include "mas/Pipeline.hpp"
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    if (argc < 3) {
        std::cerr << "usage: clean <raw_in.csv> <events_out.csv|events_out.duckdb> [machine_id]\n";
        return 2;
    }
    const std::string in = argv[1], out = argv[2];
    const std::string machine = (argc > 3) ? argv[3] : "MCC";

    if (std::string_view(out).ends_with(".duckdb")) {
        try {
            mas::DuckDbEventStore store(out, machine);
            const long long n = mas::clean_file(in, store);
            if (n == -1) {
                std::cerr << "error: cannot open input file " << in << "\n";
                return 1;
            }
            std::cerr << "wrote " << n << " cap events; store now holds "
                      << store.count() << " rows\n";
        } catch (const std::exception& e) {
            std::cerr << "error: " << e.what() << "\n";
            return 1;
        }
        return 0;
    }

    const long long n = mas::clean_file(in, out, machine);
    if (n == -1) {
        std::cerr << "error: cannot open input file " << in << "\n";
        return 1;
    }
    if (n < 0) {
        std::cerr << "error: cannot write output file " << out << "\n";
        return 1;
    }
    std::cerr << "wrote " << n << " cap events\n";
    return 0;
}
