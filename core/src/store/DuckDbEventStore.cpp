#include "mas/store/DuckDbEventStore.hpp"
#include "mas/store/DuckDbExec.hpp"
#include "mas/store/SqlQuote.hpp"
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

DuckDbEventStore::DuckDbEventStore(const std::string& db_path,
                                   const std::string& machine_id) {
    try {
        impl_ = std::make_unique<Impl>(db_path, machine_id);
    } catch (const std::exception& e) {
        throw std::runtime_error("cannot open DuckDB database " + db_path + ": " + e.what());
    }
    // Spec §6 schema, plus is_reset: a counter reset is a CapEvent carrying a
    // marker, not a row type of its own.
    //
    // The identity is (machine_id, head_id, ts), NOT (..., cap_seq). cap_seq is
    // the PLC's Count register, and the register resets. Measured on this pool:
    // head 1 alone has 23,851 day-17 closures whose cap_seq was already used on
    // days 1-15, and 18,721 of them carry a *different* torque -- distinct
    // physical closures, not retransmissions. Keying on cap_seq collapsed them
    // onto the older rows -- 34% of February discarded, 63% of the quarter, and
    // which side of a collision survived left to thread scheduling, so the same
    // input gave the same count and different contents. The row counts on both
    // sides of the rebuild are in docs/validation-log.md, 2026-08-11 "Event
    // identity"; quoting them here is how they go stale.
    //
    // ts is the physical identity of the observation and needs no bookkeeping:
    // the extractor emits at most one event per head per poll (caps missed
    // between polls are folded into one event with delta > 1), timestamps are
    // unique within a day-file (86,399 rows, 86,399 distinct ts), and the
    // day-files are contiguous and non-overlapping. Reprocessing a file still
    // deduplicates exactly, so §10 idempotency holds -- without an epoch
    // counter, and without forcing the MAS to process files in timestamp order.
    exec_or_throw(impl_->con, R"sql(
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
    exec_or_throw(impl_->con,
        "CREATE TABLE IF NOT EXISTS store_meta (key VARCHAR PRIMARY KEY, value VARCHAR)");
    {
        auto tagged = query_or_throw(impl_->con,
            "SELECT value FROM store_meta WHERE key = 'event_identity'");
        if (tagged->RowCount() == 0) {
            // Ask the live index, not the row count: an old file whose first
            // run died before writing anything is still keyed on cap_seq, and
            // the stamp written below is irreversible, so a wrong answer here
            // is permanent for that file.
            //
            // The probe is two inserts differing only in ts: the current key
            // accepts both, UNIQUE(..., cap_seq) rejects the second. Doing it by
            // behaviour rather than by reading duckdb_constraints() keeps the
            // check independent of the catalog's shape across DuckDB versions,
            // and tests the property the numbers actually depend on. Splitting
            // it into two statements is what separates "keyed on cap_seq" from
            // "this file is not a cap_events store at all": the first insert
            // failing is a schema problem and is reported as itself.
            //
            // Narrower than it looks: the probe only asks whether the *second*
            // insert is rejected, so a cap_events table carrying no unique
            // constraint at all accepts both, passes, and is stamped as
            // migrated. That file is not one any version of this code produced,
            // and the alternative is reading duckdb_constraints(), whose shape
            // across DuckDB versions is exactly what this probe avoids.
            exec_or_throw(impl_->con, "BEGIN TRANSACTION");
            static constexpr const char* kProbe =
                "INSERT INTO cap_events VALUES ('__mas_identity_probe__',-1,";
            auto first = impl_->con.Query(
                std::string(kProbe) + "'2000-01-01 00:00:00',-1,0,0,0,false,false,false)");
            if (first->HasError()) {
                const std::string err = first->GetError();
                impl_->con.Query("ROLLBACK");
                throw std::runtime_error(
                    "store " + db_path + " does not hold a usable cap_events table: " + err);
            }
            auto second = impl_->con.Query(
                std::string(kProbe) + "'2000-01-01 00:00:01',-1,0,0,0,false,false,false)");
            const bool keyed_on_cap_seq = second->HasError();
            exec_or_throw(impl_->con, "ROLLBACK");
            if (keyed_on_cap_seq)
                throw std::runtime_error(
                    "store " + db_path + " predates the event-identity fix: it is "
                    "keyed on cap_seq, which collapses distinct closures across "
                    "counter resets. Delete it and rebuild with scripts/build_store.sh");
            exec_or_throw(impl_->con,
                "INSERT INTO store_meta VALUES ('event_identity','machine_id,head_id,ts')");
        }
    }
    // Staging table for batched appends; ts kept VARCHAR here, cast on merge.
    exec_or_throw(impl_->con, R"sql(
        CREATE TABLE IF NOT EXISTS staging_cap_events (
            machine_id VARCHAR, head_id SMALLINT, ts VARCHAR,
            cap_seq BIGINT, app_torque REAL, status REAL,
            delta INTEGER, is_fault BOOLEAN, aggregated BOOLEAN, is_reset BOOLEAN
        ))sql");
    exec_or_throw(impl_->con, "DELETE FROM staging_cap_events");  // stale rows from a crashed run
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
    //
    // The Appender above is NOT inside this transaction — it runs in
    // auto-commit, so by the time the INSERT below throws (the strict ts cast),
    // the staged rows are already durable. The catch must therefore clear
    // staging itself, or the poisoned row is re-selected by every later
    // write() and one malformed cell fails the store for the process lifetime.
    // Both statements are best-effort (con.Query, not exec_or_throw): a throw
    // from cleanup inside the catch would replace the real error with a
    // rollback error. Same form as merge_all's detach_all.
    exec_or_throw(impl_->con, "BEGIN TRANSACTION");
    try {
        exec_or_throw(impl_->con, R"sql(
            INSERT OR IGNORE INTO cap_events (machine_id, head_id, ts, cap_seq, app_torque, status, delta, is_fault, aggregated, is_reset)
            SELECT machine_id, head_id, CAST(ts AS TIMESTAMP), cap_seq, app_torque,
                   status, delta, is_fault, aggregated, is_reset
            FROM staging_cap_events)sql");
        exec_or_throw(impl_->con, "DELETE FROM staging_cap_events");
    } catch (...) {
        auto rb = impl_->con.Query("ROLLBACK");                       // best effort
        (void)rb;
        auto del = impl_->con.Query("DELETE FROM staging_cap_events"); // do not poison the next write
        (void)del;
        throw;
    }
    exec_or_throw(impl_->con, "COMMIT");
}

long long DuckDbEventStore::count() const {
    auto res = query_or_throw(impl_->con, "SELECT COUNT(*) FROM cap_events");
    return res->GetValue(0, 0).GetValue<int64_t>();
}

void DuckDbEventStore::merge_from(const std::string& other_db_path) {
    exec_or_throw(impl_->con, "ATTACH '" + sql_quote(other_db_path) + "' AS src (READ_ONLY)");
    try {
        exec_or_throw(impl_->con, R"sql(
            INSERT OR IGNORE INTO cap_events (machine_id, head_id, ts, cap_seq, app_torque, status, delta, is_fault, aggregated, is_reset)
            SELECT machine_id, head_id, ts, cap_seq, app_torque, status, delta, is_fault, aggregated, is_reset
            FROM src.cap_events)sql");
    } catch (...) {
        // The alias must not survive a failed merge: mas_merge skips and
        // continues past an unopenable source, so a left-attached "src" from
        // one corrupt-but-attachable store would poison every later
        // ATTACH ... AS src in the same loop. Best effort: a DETACH failure
        // inside the catch must not mask the merge error itself.
        auto res = impl_->con.Query("DETACH src");
        (void)res;
        throw;
    }
    exec_or_throw(impl_->con, "DETACH src");
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
            exec_or_throw(impl_->con, "ATTACH '" + sql_quote(other_db_paths[i]) +
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
        exec_or_throw(impl_->con,
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
