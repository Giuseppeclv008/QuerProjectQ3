#pragma once
#include <duckdb.hpp>
#include <stdexcept>
#include <string>

namespace mas {

// DuckDB's C++ API reports failure in the result object rather than by
// throwing, so every call site has to check `HasError()` -- and a call site
// that forgets carries on with a result that holds nothing. Three stores each
// grew their own copy of this check in an anonymous namespace; they are the
// same three lines, and the next store would have written them a fourth time.
// Extracted for the same reason SqlQuote.hpp was: before a second user needs
// it, not after a third.

// Run `sql` for its effect. Throws std::runtime_error carrying DuckDB's own
// message, which names the statement and the position.
inline void exec_or_throw(duckdb::Connection& con, const std::string& sql) {
    auto res = con.Query(sql);
    if (res->HasError()) throw std::runtime_error(res->GetError());
}

// Same check, but hands back the materialized result for callers that need the
// rows rather than just the success.
inline std::unique_ptr<duckdb::MaterializedQueryResult> query_or_throw(
        duckdb::Connection& con, const std::string& sql) {
    auto res = con.Query(sql);
    if (res->HasError()) throw std::runtime_error(res->GetError());
    return res;
}

// The single-value case: COUNT(*) and friends. Reading (0,0) off an empty
// result is undefined, so the aggregate that produced it must be one that
// always returns a row.
inline long long scalar_or_throw(duckdb::Connection& con, const std::string& sql) {
    return query_or_throw(con, sql)->GetValue(0, 0).GetValue<int64_t>();
}

} // namespace mas
