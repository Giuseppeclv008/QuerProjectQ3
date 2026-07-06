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
    execOrThrow(impl_->con, R"sql(
        INSERT OR IGNORE INTO cap_events
        SELECT machine_id, head_id, CAST(ts AS TIMESTAMP), cap_seq, app_torque,
               status, delta, is_fault, aggregated, is_reset
        FROM staging_cap_events)sql");
    execOrThrow(impl_->con, "DELETE FROM staging_cap_events");
}

long long DuckDbEventStore::count() const {
    auto res = impl_->con.Query("SELECT COUNT(*) FROM cap_events");
    if (res->HasError()) throw std::runtime_error(res->GetError());
    return res->GetValue(0, 0).GetValue<int64_t>();
}

void DuckDbEventStore::export_parquet(const std::string&) {
    throw std::runtime_error("export_parquet: implemented in Task 5");
}

} // namespace mas
