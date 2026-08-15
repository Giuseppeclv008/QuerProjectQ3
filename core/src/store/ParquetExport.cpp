#include "mas/store/ParquetExport.hpp"
#include "mas/store/DuckDbExec.hpp"
#include "mas/store/SqlQuote.hpp"
#include <duckdb.hpp>
#include <filesystem>
#include <stdexcept>

namespace mas {
namespace {

// "2026-02-02" carries no time, so SQL reads it as 2026-02-02 00:00:00 and an
// upper bound written that way silently drops that entire day -- the export
// still succeeds and still reports a count, so nobody learns the day is
// missing. A date alone therefore means the end of that day on the upper
// bound. On the lower bound midnight is already what "from the 2nd" means, so
// it is left alone. An explicit time on either side is used exactly as given.
bool is_date_only(const std::string& s) {
    return s.find(':') == std::string::npos &&
           s.find(' ') == std::string::npos &&
           s.find('T') == std::string::npos;
}

// ts BETWEEN ... AND ..., but with either side optionally absent.
std::string where_clause(const std::string& since, const std::string& until) {
    std::string w;
    if (!since.empty()) w += " WHERE ts >= TIMESTAMP '" + sql_quote(since) + "'";
    if (!until.empty()) {
        const std::string upper =
            is_date_only(until) ? until + " 23:59:59.999999" : until;
        w += (w.empty() ? " WHERE" : " AND") +
             std::string(" ts <= TIMESTAMP '") + sql_quote(upper) + "'";
    }
    return w;
}

} // namespace

ExportResult export_store_to_parquet(const std::string& db_path,
                                     const std::string& out_path,
                                     const std::string& since,
                                     const std::string& until) {
    if (!std::filesystem::exists(db_path))
        throw std::runtime_error("no such store: " + db_path);

    // COPY ... TO truncates its destination, and it does not care that the
    // destination is the database it is reading. `mas_export s.duckdb s.duckdb`
    // overwrote the store with the Parquet of its own contents, and then the
    // verification below read that Parquet back, found the count it expected,
    // and exited 0 -- silent destruction of the format this project persists
    // into, reported as success.
    std::error_code ec;
    if (std::filesystem::exists(out_path) &&
        std::filesystem::equivalent(db_path, out_path, ec) && !ec)
        throw std::runtime_error("refusing to export " + db_path +
                                 " onto itself: the destination is the store");
    if (std::filesystem::exists(out_path))
        throw std::runtime_error("refusing to overwrite " + out_path +
                                 ": delete it first, or export to a new path");
    // Both guards above turn on the destination already existing, which leaves
    // one path they cannot see: the store's write-ahead log before DuckDB has
    // created it. Writing a Parquet file to <store>.wal is not destructive
    // today -- DuckDB rejected the bogus log and the rows survived -- but
    // mas_merge documents a WAL precondition, and a file that looks like a log
    // and is not one is a trap laid for whoever hits that path next.
    // weakly_canonical, not string equality: `equivalent()` three lines up
    // cannot serve here because it needs both files to exist, and the whole
    // point of this guard is the WAL that does not exist yet. A raw comparison
    // let `mas_export ./s.duckdb s.duckdb.wal` straight through -- one "./" and
    // the two strings differ while the paths do not. weakly_canonical resolves
    // the spelling without requiring the target to exist.
    //
    // One error_code each, and both checked: sharing one hid whether the
    // *destination* resolved, and an unresolvable destination compares equal to
    // nothing -- which silently turns the guard off in precisely the cases
    // (ELOOP, an unreadable intermediate) where it is least safe to assume.
    // Falling back to the raw comparison keeps the common spelling covered
    // rather than trading one blind spot for another.
    std::error_code wal_ec, dest_ec;
    const auto wal = std::filesystem::weakly_canonical(db_path + ".wal", wal_ec);
    const auto dest = std::filesystem::weakly_canonical(out_path, dest_ec);
    const bool resolved = !wal_ec && !dest_ec && !wal.empty() && !dest.empty();
    if (resolved ? wal == dest : out_path == db_path + ".wal")
        throw std::runtime_error("refusing to write " + out_path +
                                 ": that is the store's write-ahead log");

    const auto parent = std::filesystem::path(out_path).parent_path();
    if (!parent.empty()) {
        std::error_code ec;
        std::filesystem::create_directories(parent, ec);
        if (ec && !std::filesystem::is_directory(parent))
            throw std::runtime_error("cannot create directory " + parent.string() +
                                     ": " + ec.message());
    }

    // READ_ONLY is load-bearing, not defensive: it is what lets this run
    // against a store on read-only media, and what guarantees the export
    // cannot become a writer of the thing it is reading.
    duckdb::DBConfig cfg;
    cfg.options.access_mode = duckdb::AccessMode::READ_ONLY;
    std::unique_ptr<duckdb::DuckDB> db;
    try {
        db = std::make_unique<duckdb::DuckDB>(db_path, &cfg);
    } catch (const std::exception& e) {
        throw std::runtime_error("cannot open " + db_path + " read-only: " + e.what());
    }
    duckdb::Connection con(*db);

    const std::string where = where_clause(since, until);
    const long long expected = scalar_or_throw(con, "SELECT COUNT(*) FROM cap_events" + where);

    // ORDER BY makes the file deterministic: same store, same bytes. Without
    // it DuckDB is free to emit row groups in whatever order the scan produced,
    // so two exports of one store would not compare equal.
    exec_or_throw(con, "COPY (SELECT * FROM cap_events" + where +
                     " ORDER BY head_id, ts) TO '" + sql_quote(out_path) +
                     "' (FORMAT PARQUET)");

    // Verify against the file just written rather than trusting COPY. A
    // truncated or unreadable Parquet must fail here, not three months later
    // when somebody tries to read it.
    const long long written =
        scalar_or_throw(con, "SELECT COUNT(*) FROM read_parquet('" + sql_quote(out_path) + "')");
    if (written != expected)
        throw std::runtime_error("export verification failed for " + out_path +
                                 ": store has " + std::to_string(expected) +
                                 " rows in range, Parquet holds " +
                                 std::to_string(written));

    return ExportResult{expected, out_path};
}

} // namespace mas
