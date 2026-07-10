#include "mas/DuckDbEventStore.hpp"
#include <duckdb.hpp>
#include <stdexcept>

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

} // namespace

DuckDbEventStore::DuckDbEventStore(const std::string& db_path,
                                   const std::string& machine_id) {
    try {
        impl_ = std::make_unique<Impl>(db_path, machine_id);
    } catch (const std::exception& e) {
        throw std::runtime_error("cannot open DuckDB database " + db_path + ": " + e.what());
    }
    // Spec §6 schema; is_reset added (Plan 1 folded ResetMarker into CapEvent).
    execOrThrow(impl_->con, R"sql(
        CREATE TABLE IF NOT EXISTS cap_events (
            machine_id VARCHAR NOT NULL,
            head_id    SMALLINT NOT NULL,
            ts         TIMESTAMP,
            cap_seq    BIGINT NOT NULL,
            app_torque REAL,
            status     REAL,
            delta      INTEGER,
            is_fault   BOOLEAN,
            aggregated BOOLEAN,
            is_reset   BOOLEAN,
            UNIQUE (machine_id, head_id, cap_seq)
        ))sql");
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
    execOrThrow(impl_->con, R"sql(
        INSERT OR IGNORE INTO cap_events (machine_id, head_id, ts, cap_seq, app_torque, status, delta, is_fault, aggregated, is_reset)
        SELECT machine_id, head_id, CAST(ts AS TIMESTAMP), cap_seq, app_torque,
               status, delta, is_fault, aggregated, is_reset
        FROM staging_cap_events)sql");
    execOrThrow(impl_->con, "DELETE FROM staging_cap_events");
}

long long DuckDbEventStore::count() const {
    auto res = queryOrThrow(impl_->con, "SELECT COUNT(*) FROM cap_events");
    return res->GetValue(0, 0).GetValue<int64_t>();
}

void DuckDbEventStore::export_parquet(const std::string& parquet_path) {
    // Note: path is spliced into SQL — fine for trusted local paths, but a
    // path containing a single quote would break the statement.
    execOrThrow(impl_->con,
        "COPY (SELECT * FROM cap_events ORDER BY head_id, ts) TO '" +
        parquet_path + "' (FORMAT PARQUET)");
}

void DuckDbEventStore::merge_from(const std::string& other_db_path) {
    // Same trusted-local-path caveat as export_parquet: the path is spliced
    // into SQL, so a quote in it would break the statement.
    execOrThrow(impl_->con, "ATTACH '" + other_db_path + "' AS src (READ_ONLY)");
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

} // namespace mas
