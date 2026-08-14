#pragma once
#include <string>

namespace mas {

// ATTACH, COPY and read_parquet all take a path as a SQL string literal, and
// DuckDB has no parameter binding for any of them. Doubling embedded quotes is
// the escape SQL defines; without it a path like /tmp/o'brien/store.duckdb
// terminates the literal early and the statement fails with a parse error
// nobody can read.
inline std::string sql_quote(const std::string& s) {
    std::string out;
    out.reserve(s.size());
    for (const char c : s) {
        out.push_back(c);
        if (c == '\'') out.push_back('\'');
    }
    return out;
}

} // namespace mas
