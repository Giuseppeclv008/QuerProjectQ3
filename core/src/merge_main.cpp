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
        for (int i = 3; i < argc; ++i) dst.merge_from(argv[i]);
        std::cerr << "merged " << (argc - 3) << " stores; dst holds "
                  << dst.count() << " rows\n";
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << "\n";
        return 1;
    }
    return 0;
}
