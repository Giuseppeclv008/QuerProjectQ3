#include "mas/store/ParquetEventStore.hpp"
#include "mas/store/SqlQuote.hpp"
#include <duckdb.hpp>
#include <filesystem>
#include <iostream>
#include <stdexcept>

namespace mas {

struct ParquetEventStore::Impl {
    duckdb::DuckDB db;          // in-memory: no file, no WAL, no checkpoint
    duckdb::Connection con;
    std::string path;
    std::string machine_id;
    long long n = 0;
    bool closed = false;
    Impl(std::string p, std::string mid)
        : db(nullptr), con(db), path(std::move(p)), machine_id(std::move(mid)) {}
};

namespace {

void execOrThrow(duckdb::Connection& con, const std::string& sql) {
    auto res = con.Query(sql);
    if (res->HasError()) throw std::runtime_error(res->GetError());
}

} // namespace

ParquetEventStore::ParquetEventStore(const std::string& out_path,
                                     const std::string& machine_id) {
    const auto parent = std::filesystem::path(out_path).parent_path();
    if (!parent.empty()) {
        std::error_code ec;
        std::filesystem::create_directories(parent, ec);
        if (ec && !std::filesystem::is_directory(parent))
            throw std::runtime_error("cannot create directory " + parent.string() +
                                     ": " + ec.message());
    }
    impl_ = std::make_unique<Impl>(out_path, machine_id);
    // Same ten columns and types as cap_events, minus the constraint. The
    // absence of UNIQUE is the whole experiment.
    execOrThrow(impl_->con, R"sql(
        CREATE TABLE buf (
            machine_id VARCHAR NOT NULL,
            head_id    SMALLINT NOT NULL,
            ts         TIMESTAMP NOT NULL,
            cap_seq    BIGINT NOT NULL,
            app_torque REAL,
            status     REAL,
            delta      INTEGER,
            is_fault   BOOLEAN,
            aggregated BOOLEAN,
            is_reset   BOOLEAN
        ))sql");
}

ParquetEventStore::~ParquetEventStore() {
    try {
        close();
    } catch (const std::exception& e) {
        // A destructor cannot report failure, and throwing from one during
        // stack unwinding terminates. Say it loudly instead; callers that need
        // the error call close() themselves.
        std::cerr << "error: writing " << impl_->path << ": " << e.what() << "\n";
    }
}

void ParquetEventStore::write(std::span<const CapEvent> events) {
    if (events.empty()) return;
    duckdb::Appender app(impl_->con, "buf");
    for (const auto& e : events) {
        app.BeginRow();
        app.Append(duckdb::Value(impl_->machine_id));
        app.Append(duckdb::Value::SMALLINT(static_cast<int16_t>(e.head_id)));
        app.Append(duckdb::Value(e.ts));              // VARCHAR -> TIMESTAMP cast
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
    impl_->n += static_cast<long long>(events.size());
}

void ParquetEventStore::close() {
    if (impl_->closed) return;
    impl_->closed = true;
    // Written even when empty: a day-file that yields no events must still
    // leave a file with the right schema, or a later read_parquet over the
    // directory fails because of it.
    execOrThrow(impl_->con,
        "COPY (SELECT * FROM buf ORDER BY head_id, ts) TO '" +
        sql_quote(impl_->path) + "' (FORMAT PARQUET)");
}

long long ParquetEventStore::count() const { return impl_->n; }

std::string parquet_path_for(const std::string& out_dir, const std::string& in_path) {
    return (std::filesystem::path(out_dir) /
            (std::filesystem::path(in_path).stem().string() + ".parquet")).string();
}

} // namespace mas
