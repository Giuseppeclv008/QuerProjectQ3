#include "mas/DuckDbEventStore.hpp"
#include <exception>
#include <iostream>

int main(int argc, char** argv) {
    if (argc < 4) {
        std::cerr << "usage: mas_merge <dst.duckdb> <machine_id> <src1.duckdb> [src2.duckdb ...]\n";
        return 2;
    }
    try {
        mas::DuckDbEventStore dst(argv[1], argv[2]);
        int merged = 0, skipped = 0;
        for (int i = 3; i < argc; ++i) {
            // A crashed worker's store may be unreadable; the resilience
            // design writes it off (its items were re-dispatched), so a
            // failed source is skipped loudly instead of aborting the merge.
            try {
                dst.merge_from(argv[i]);
                ++merged;
            } catch (const std::exception& e) {
                std::cerr << "skip " << argv[i] << ": " << e.what() << "\n";
                ++skipped;
            }
        }
        std::cerr << "merged " << merged << " stores (" << skipped
                  << " skipped); dst holds " << dst.count() << " rows\n";
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << "\n";
        return 1;
    }
    return 0;
}
