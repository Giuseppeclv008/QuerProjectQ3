#include "mas/store/ParquetExport.hpp"
#include "mas/store/AtomicPublish.hpp"
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
    // The non-throwing overload throughout this function. The throwing one
    // leaks std::filesystem_error past every message below: a store behind a
    // symlink loop or an unreadable directory answered with a raw
    // "in posix_stat: failed to determine attributes", naming neither the guard
    // that would have refused it nor what the caller should do. Worse, it made
    // the guards further down unreachable for exactly the paths they exist to
    // catch, which is how a test of the WAL guard came to pass on a stat error
    // instead.
    const auto exists_or_throw = [](const std::string& p, const char* what) {
        std::error_code ec;
        const bool present = std::filesystem::exists(p, ec);
        if (ec) throw std::runtime_error(std::string("cannot examine ") + what +
                                         " " + p + ": " + ec.message());
        return present;
    };

    if (!exists_or_throw(db_path, "store"))
        throw std::runtime_error("no such store: " + db_path);

    // COPY ... TO truncates its destination, and it does not care that the
    // destination is the database it is reading. `mas_export s.duckdb s.duckdb`
    // overwrote the store with the Parquet of its own contents, and then the
    // verification below read that Parquet back, found the count it expected,
    // and exited 0 -- silent destruction of the format this project persists
    // into, reported as success.
    const bool dest_exists = exists_or_throw(out_path, "destination");
    std::error_code ec;   // equivalent()'s; exists_or_throw owns its own
    if (dest_exists &&
        std::filesystem::equivalent(db_path, out_path, ec) && !ec)
        throw std::runtime_error("refusing to export " + db_path +
                                 " onto itself: the destination is the store");
    if (dest_exists)
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
    // nothing -- which would silently turn the guard off.
    //
    // The fallback is a real fallback: it is reached, it fires, and where it
    // fires the guard is weaker. Two previous versions of this comment claimed
    // otherwise -- first that no input reached it, then that it could never
    // fire -- and both were wrong by construction, so this one states the
    // behaviour instead of arguing for a bound.
    //
    // What makes weakly_canonical fail while exists() succeeds: exists() stats
    // relative to the cwd, weakly_canonical goes through the absolute form. In
    // a cwd longer than PATH_MAX both calls here set their error_code while
    // both arguments stat cleanly, and the string comparison is what runs.
    // Measured from a 1259-byte cwd on APFS:
    //
    //   mas_export s.duckdb s.duckdb.wal    -> refused, by string equality
    //   mas_export s.duckdb ./s.duckdb.wal  -> EXPORTED ONTO THE WAL, rc=0
    //
    // That second line is the honest limit of this guard: the "./" spelling is
    // precisely what weakly_canonical was added to catch, and in the state
    // where canonicalization is unavailable there is nothing left to catch it
    // with. The fallback still buys the exact spelling, which is the one a
    // script produces; it does not buy spelling-independence, and no comparison
    // here can while the paths cannot be resolved.
    //
    // Case is a second such limit, on this platform rather than in this code:
    // weakly_canonical does not case-fold, so on a case-insensitive volume
    // (APFS's default) S.DUCKDB.WAL names the same file and is not refused.
    //
    // A typo guard, then -- not a security boundary. Nothing downstream relies
    // on it: DuckDB rejects a bogus WAL on open, which is what actually keeps
    // the store safe.
    std::error_code wal_ec, dest_ec;
    const auto wal = std::filesystem::weakly_canonical(db_path + ".wal", wal_ec);
    const auto dest = std::filesystem::weakly_canonical(out_path, dest_ec);
    const bool resolved = !wal_ec && !dest_ec && !wal.empty() && !dest.empty();
    if (resolved ? wal == dest : out_path == db_path + ".wal")
        throw std::runtime_error("refusing to write " + out_path +
                                 ": that is the store's write-ahead log");

    const auto parent = std::filesystem::path(out_path).parent_path();
    if (!parent.empty()) {
        std::error_code mk_ec, dir_ec;
        std::filesystem::create_directories(parent, mk_ec);
        // Non-throwing here too: the last throwing filesystem call in this
        // function, four lines under the block that stopped the rest of them
        // leaking filesystem_error past these messages. No input reaches it --
        // parent is a prefix of out_path, which exists_or_throw already stat'd
        // -- but "unreachable" is what the WAL fallback's comment claimed too.
        if (mk_ec && !std::filesystem::is_directory(parent, dir_ec))
            throw std::runtime_error("cannot create directory " + parent.string() +
                                     ": " + mk_ec.message());
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

    // Written to a private name and renamed into place, and verified before the
    // rename rather than after. A COPY that runs out of space used to leave its
    // truncated output at out_path -- unreadable, so any glob over that
    // directory failed on its account, and the overwrite guard above then
    // refused the retry that would have replaced it. Nothing partial is ever
    // visible under the name the user chose now, and a failed export leaves the
    // destination exactly as it found it.
    publish_atomically(out_path, [&](const std::string& tmp) {
        // ORDER BY makes the file deterministic: same store, same bytes.
        // Without it DuckDB is free to emit row groups in whatever order the
        // scan produced, so two exports of one store would not compare equal.
        exec_or_throw(con, "COPY (SELECT * FROM cap_events" + where +
                         " ORDER BY head_id, ts) TO '" + sql_quote(tmp) +
                         "' (FORMAT PARQUET)");

        // Verify the bytes just written rather than trusting COPY. A truncated
        // or unreadable Parquet must fail here, not three months later when
        // somebody tries to read it -- and failing here means it never reaches
        // out_path at all.
        const long long written =
            scalar_or_throw(con, "SELECT COUNT(*) FROM read_parquet('" + sql_quote(tmp) + "')");
        if (written != expected)
            throw std::runtime_error("export verification failed for " + out_path +
                                     ": store has " + std::to_string(expected) +
                                     " rows in range, Parquet holds " +
                                     std::to_string(written));
    });

    return ExportResult{expected, out_path};
}

} // namespace mas
