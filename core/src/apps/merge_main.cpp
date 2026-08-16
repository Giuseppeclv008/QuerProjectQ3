#include "mas/store/DuckDbEventStore.hpp"
#include <exception>
#include <iostream>
#include <string>
#include <vector>

int main(int argc, char** argv) {
    if (argc < 4) {
        std::cerr << "usage: mas_merge <dst.duckdb> <machine_id> <src1.duckdb> [src2.duckdb ...]\n"
                     "  <machine_id> labels rows the destination writes itself. It does NOT\n"
                     "  relabel merged rows: merge_from copies each source's own machine_id.\n";
        return 2;
    }
    try {
        mas::DuckDbEventStore dst(argv[1], argv[2]);
        int merged = 0, skipped = 0;

        std::vector<std::string> sources(argv + 3, argv + argc);

        // Fast path first: merge_all does one set-based pass over every source
        // instead of N INSERT OR IGNORE passes each probing the UNIQUE index
        // per row. It is a single statement, so an unreadable source aborts the
        // whole thing without inserting anything — which would throw away the
        // resilience guarantee that a crashed worker's store is skipped, not
        // fatal (its items were re-dispatched). Hence the fallback, which
        // starts from the same empty destination the bulk path left behind.
        //
        // Probing each source first was the obvious alternative and is wrong:
        // opening one through DuckDbEventStore writes to it.
        try {
            dst.merge_all(sources);
            merged = static_cast<int>(sources.size());
        } catch (const std::exception& e) {
            std::cerr << "bulk merge failed (" << e.what()
                      << "); retrying one source at a time\n";
            for (const auto& src : sources) {
                try {
                    dst.merge_from(src);
                    ++merged;
                } catch (const std::exception& inner) {
                    std::cerr << "skip " << src << ": " << inner.what() << "\n";
                    ++skipped;
                }
            }
        }
        std::cerr << "merged " << merged << " stores (" << skipped
                  << " skipped); dst holds " << dst.count() << " rows\n";
        // The machine_id argument only labels rows this destination writes
        // itself; merge_from carries each source's own value across. Passing a
        // different id looked like reassigning the machine and silently did
        // nothing — chaos_e2e.sh passes MCC777eda… while its workers wrote MCC.
        std::cerr << "note: merged rows keep their source machine_id; '" << argv[2]
                  << "' labels only rows this destination writes itself\n";
        // Skipping a crashed worker's store is the designed resilience path,
        // but skipping *every* source means the run produced nothing -- exit 0
        // there made chaos_e2e.sh's `|| exit 1` guard dead code.
        if (merged == 0 && !sources.empty()) {
            std::cerr << "error: no source could be merged\n";
            return 1;
        }
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << "\n";
        return 1;
    }
    return 0;
}
