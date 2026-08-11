#include "mas/store/DuckDbEventStore.hpp"
#include <duckdb.hpp>
#include <stdexcept>
#include <vector>

namespace mas {

struct DuckDbEventStore::Impl {
    duckdb::DuckDB db;
    duckdb::Connection con;
    std::string machine_id;
    Impl(const std::string& path, std::string mid)
        : db(path), con(db), machine_id(std::move(mid)) {}
};

namespace {

void execOrThrow(duckdb::Connection& con, const std::string& sql) {
    auto res = con.Query(sql);
    if (res->HasError()) throw std::runtime_error(res->GetError());
}

// Same as execOrThrow, but returns the materialized result for callers that
// need the data back (e.g. count()) instead of just checking for success.
auto queryOrThrow(duckdb::Connection& con, const std::string& sql) {
    auto res = con.Query(sql);
    if (res->HasError()) throw std::runtime_error(res->GetError());
    return res;
}

// ATTACH and COPY take a path as a SQL string literal, and DuckDB has no
// parameter binding for either. Doubling embedded quotes is the escape SQL
// defines; without it a path like /tmp/o'brien/store.duckdb terminates the
// literal early and the statement fails with a parse error nobody can read.
std::string sql_quote(const std::string& s) {
    std::string out;
    out.reserve(s.size());
    for (const char c : s) {
        out.push_back(c);
        if (c == '\'') out.push_back('\'');
    }
    return out;
}

} // namespace

DuckDbEventStore::DuckDbEventStore(const std::string& db_path,
                                   const std::string& machine_id) {
    try {
        impl_ = std::make_unique<Impl>(db_path, machine_id);
    } catch (const std::exception& e) {
        throw std::runtime_error("cannot open DuckDB database " + db_path + ": " + e.what());
    }
    // Spec §6 schema; is_reset added (Plan 1 folded ResetMarker into CapEvent).
    //
    // The identity is (machine_id, head_id, ts), NOT (..., cap_seq). cap_seq is
    // the PLC's Count register, and the register resets. Measured on this pool:
    // head 1 alone has 23,851 day-17 closures whose cap_seq was already used on
    // days 1-15, and 18,721 of them carry a *different* torque -- distinct
    // physical closures, not retransmissions. Keying on cap_seq collapsed them
    // onto the older rows: 21,872,663 February events persisted as 14,372,237
    // rows, and February inside the three-month store held only 10,450,551 --
    // 3.9M February closures evicted by March/April rows reusing their counter
    // values, with which ones survived decided by thread scheduling.
    //
    // ts is the physical identity of the observation and needs no bookkeeping:
    // the extractor emits at most one event per head per poll (caps missed
    // between polls are folded into one event with delta > 1), timestamps are
    // unique within a day-file (86,399 rows, 86,399 distinct ts), and the
    // day-files are contiguous and non-overlapping. Reprocessing a file still
    // deduplicates exactly, so §10 idempotency holds -- without an epoch
    // counter, and without forcing the MAS to process files in timestamp order.
    execOrThrow(impl_->con, R"sql(
        CREATE TABLE IF NOT EXISTS cap_events (
            machine_id VARCHAR NOT NULL,
            head_id    SMALLINT NOT NULL,
            ts         TIMESTAMP NOT NULL,
            cap_seq    BIGINT NOT NULL,
            app_torque REAL,
            status     REAL,
            delta      INTEGER,
            is_fault   BOOLEAN,
            aggregated BOOLEAN,
            is_reset   BOOLEAN,
            UNIQUE (machine_id, head_id, ts)
        ))sql");
    // A store written before this change carries UNIQUE(..., cap_seq), and
    // CREATE TABLE IF NOT EXISTS would silently keep it -- every downstream
    // number would stay wrong with nothing to show for it. Refuse the file.
    execOrThrow(impl_->con,
        "CREATE TABLE IF NOT EXISTS store_meta (key VARCHAR PRIMARY KEY, value VARCHAR)");
    {
        auto tagged = queryOrThrow(impl_->con,
            "SELECT value FROM store_meta WHERE key = 'event_identity'");
        if (tagged->RowCount() == 0) {
            auto rows = queryOrThrow(impl_->con, "SELECT COUNT(*) FROM cap_events");
            if (rows->GetValue(0, 0).GetValue<int64_t>() > 0)
                throw std::runtime_error(
                    "store " + db_path + " predates the event-identity fix: it is "
                    "keyed on cap_seq, which collapses distinct closures across "
                    "counter resets. Delete it and rebuild with scripts/build_store.sh");
            execOrThrow(impl_->con,
                "INSERT INTO store_meta VALUES ('event_identity','machine_id,head_id,ts')");
        }
    }
    // Staging table for batched appends; ts kept VARCHAR here, cast on merge.
    execOrThrow(impl_->con, R"sql(
        CREATE TABLE IF NOT EXISTS staging_cap_events (
            machine_id VARCHAR, head_id SMALLINT, ts VARCHAR,
            cap_seq BIGINT, app_torque REAL, status REAL,
            delta INTEGER, is_fault BOOLEAN, aggregated BOOLEAN, is_reset BOOLEAN
        ))sql");
    execOrThrow(impl_->con, "DELETE FROM staging_cap_events");  // stale rows from a crashed run
}

DuckDbEventStore::~DuckDbEventStore() = default;

void DuckDbEventStore::write(std::span<const CapEvent> events) {
    if (events.empty()) return;
    {
        duckdb::Appender app(impl_->con, "staging_cap_events");
        for (const auto& e : events) {
            app.BeginRow();
            app.Append(duckdb::Value(impl_->machine_id));
            app.Append(duckdb::Value::SMALLINT(static_cast<int16_t>(e.head_id)));
            app.Append(duckdb::Value(e.ts));
            app.Append(duckdb::Value::BIGINT(e.cap_seq));
            app.Append(duckdb::Value::FLOAT(static_cast<float>(e.app_torque)));
            app.Append(duckdb::Value::FLOAT(static_cast<float>(e.status)));
            app.Append(duckdb::Value::INTEGER(e.delta));
            app.Append(duckdb::Value::BOOLEAN(e.is_fault));
            app.Append(duckdb::Value::BOOLEAN(e.aggregated));
            app.Append(duckdb::Value::BOOLEAN(e.reset));
            app.EndRow();
        }
        app.Close();
    }
    // Idempotent merge: UNIQUE key drops rows already in cap_events.
    // Interim policy: the ts cast is strict — one malformed timestamp aborts
    // the day-file loudly (no partial corruption; reprocessing is idempotent).
    // Quarantine/skip-and-count belongs to the ingestion agent (spec §10);
    // revisit with TRY_CAST + counter in that plan.
    //
    // INSERT and the staging cleanup share one transaction. Without it they are
    // two auto-commit statements, and an error between them leaves staging
    // populated for the next write() to re-insert. That was idempotent by luck
    // (INSERT OR IGNORE), not by design; here it is by design.
    execOrThrow(impl_->con, "BEGIN TRANSACTION");
    try {
        execOrThrow(impl_->con, R"sql(
            INSERT OR IGNORE INTO cap_events (machine_id, head_id, ts, cap_seq, app_torque, status, delta, is_fault, aggregated, is_reset)
            SELECT machine_id, head_id, CAST(ts AS TIMESTAMP), cap_seq, app_torque,
                   status, delta, is_fault, aggregated, is_reset
            FROM staging_cap_events)sql");
        execOrThrow(impl_->con, "DELETE FROM staging_cap_events");
    } catch (...) {
        execOrThrow(impl_->con, "ROLLBACK");
        throw;
    }
    execOrThrow(impl_->con, "COMMIT");
}

long long DuckDbEventStore::count() const {
    auto res = queryOrThrow(impl_->con, "SELECT COUNT(*) FROM cap_events");
    return res->GetValue(0, 0).GetValue<int64_t>();
}

void DuckDbEventStore::export_parquet(const std::string& parquet_path) {
    execOrThrow(impl_->con,
        "COPY (SELECT * FROM cap_events ORDER BY head_id, ts) TO '" +
        sql_quote(parquet_path) + "' (FORMAT PARQUET)");
}

void DuckDbEventStore::merge_from(const std::string& other_db_path) {
    execOrThrow(impl_->con, "ATTACH '" + sql_quote(other_db_path) + "' AS src (READ_ONLY)");
    try {
        execOrThrow(impl_->con, R"sql(
            INSERT OR IGNORE INTO cap_events (machine_id, head_id, ts, cap_seq, app_torque, status, delta, is_fault, aggregated, is_reset)
            SELECT machine_id, head_id, ts, cap_seq, app_torque, status, delta, is_fault, aggregated, is_reset
            FROM src.cap_events)sql");
    } catch (...) {
        // The alias must not survive a failed merge: mas_merge (Task 5) now
        // skips-and-continues past an unopenable source, so a left-attached
        // "src" from one corrupt-but-attachable store would poison every
        // later ATTACH ... AS src in the same loop.
        execOrThrow(impl_->con, "DETACH src");
        throw;
    }
    execOrThrow(impl_->con, "DETACH src");
}

void DuckDbEventStore::merge_all(const std::vector<std::string>& other_db_paths) {
    if (other_db_paths.empty()) return;
    if (other_db_paths.size() == 1) { merge_from(other_db_paths[0]); return; }

    // The bulk path deduplicates the union of the sources against itself, not
    // against rows already present, so it is only valid on an empty table.
    if (count() > 0) {
        for (const auto& p : other_db_paths) merge_from(p);
        return;
    }

    static constexpr const char* kCols =
        "machine_id, head_id, ts, cap_seq, app_torque, status, delta, "
        "is_fault, aggregated, is_reset";

    std::vector<std::string> aliases;
    aliases.reserve(other_db_paths.size());
    auto detach_all = [&] {
        for (const auto& a : aliases) {
            auto res = impl_->con.Query("DETACH " + a);   // best effort while unwinding
            (void)res;
        }
    };

    try {
        std::string un;
        for (std::size_t i = 0; i < other_db_paths.size(); ++i) {
            const std::string alias = "msrc" + std::to_string(i);
            execOrThrow(impl_->con, "ATTACH '" + sql_quote(other_db_paths[i]) +
                                    "' AS " + alias + " (READ_ONLY)");
            aliases.push_back(alias);
            if (i) un += " UNION ALL ";
            un += "SELECT " + std::string(kCols) + " FROM " + alias + ".cap_events";
        }
        // One hash aggregation over the whole union, then one bulk append, in
        // place of N sequential INSERT OR IGNORE passes each probing the UNIQUE
        // index per row against a destination that keeps growing.
        //
        // No ORDER BY on the DISTINCT ON: duplicates only arise from a
        // re-dispatched file, and those rows agree in every column, so which
        // one survives cannot be observed.
        execOrThrow(impl_->con,
            "INSERT INTO cap_events (" + std::string(kCols) + ") "
            "SELECT DISTINCT ON (machine_id, head_id, ts) " + std::string(kCols) +
            " FROM (" + un + ")");
    } catch (...) {
        detach_all();
        throw;
    }
    detach_all();
}

} // namespace mas
