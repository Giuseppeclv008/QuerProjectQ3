#include "mas/store/ParquetExport.hpp"
#include <exception>
#include <iostream>
#include <string>

namespace {

// --since/--until each consume the next argument; anything else is positional.
bool take_value(int argc, char** argv, int& i, const char* flag, std::string& out) {
    if (std::string(argv[i]) != flag) return false;
    if (i + 1 >= argc) throw std::runtime_error(std::string(flag) + " needs a value");
    out = argv[++i];
    return true;
}

} // namespace

int main(int argc, char** argv) {
    std::string store, out, since, until;
    try {
        for (int i = 1; i < argc; ++i) {
            if (take_value(argc, argv, i, "--since", since)) continue;
            if (take_value(argc, argv, i, "--until", until)) continue;
            if (store.empty()) store = argv[i];
            else if (out.empty()) out = argv[i];
            else throw std::runtime_error(std::string("unexpected argument: ") + argv[i]);
        }
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << "\n";
        return 2;
    }

    if (store.empty() || out.empty()) {
        std::cerr <<
            "usage: mas_export <store.duckdb> <out.parquet> [--since TS] [--until TS]\n"
            "\n"
            "  Export a store's cap_events to Parquet, for handing the data to\n"
            "  something that does not read .duckdb. DuckDB remains the format the\n"
            "  system persists into; this only reads.\n"
            "\n"
            "  The store is opened READ-ONLY and is not modified.\n"
            "\n"
            "  --since/--until bound ts inclusively. A bare date as the upper bound\n"
            "  covers that whole day, so '--since 2026-02-03 --until 2026-02-03'\n"
            "  exports the 3rd; give a time to bound it exactly. A range matching\n"
            "  no rows writes a valid empty Parquet with the schema, not an error.\n"
            "\n"
            "  The written file is read back and its row count checked against the\n"
            "  store before this exits 0.\n";
        return 2;
    }

    try {
        const auto r = mas::export_store_to_parquet(store, out, since, until);
        std::cerr << "exported " << r.rows << " rows to " << r.path << "\n";
        if (r.rows == 0)
            std::cerr << "note: no rows matched"
                      << (since.empty() && until.empty() ? " (the store is empty)"
                                                         : " the requested range")
                      << "; the file carries the schema and nothing else\n";
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << "\n";
        return 1;
    }
    return 0;
}
