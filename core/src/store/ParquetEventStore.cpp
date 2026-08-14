#include "mas/store/ParquetEventStore.hpp"
#include "mas/store/DuckDbExec.hpp"
#include "mas/store/SqlQuote.hpp"
#include <duckdb.hpp>
#include <filesystem>
#include <iostream>
#include <optional>
#include <stdexcept>
#ifdef _WIN32
#include <process.h>
#else
#include <unistd.h>
#endif

namespace mas {

struct ParquetEventStore::Impl {
    duckdb::DuckDB db;          // in-memory: no file, no WAL, no checkpoint
    duckdb::Connection con;
    std::string path;
    std::string machine_id;
    // One Appender for the store's life, not one per write(). Constructing it
    // does a catalog lookup and binds the table's types, and write() is called
    // once per 8,192-event batch -- about 2,670 times per day-file. Held by
    // optional because the table has to exist first, so it cannot be built in
    // the member init list.
    std::optional<duckdb::Appender> appender;
    long long n = 0;
    bool closed = false;
    Impl(std::string p, std::string mid)
        : db(nullptr), con(db), path(std::move(p)), machine_id(std::move(mid)) {}
};

namespace {

// What makes the temp name private to this process, which is the granularity
// that matters: the two writers that can collide are a tombstoned worker and
// its replacement, in separate processes.
int process_id() {
#ifdef _WIN32
    return _getpid();
#else
    return static_cast<int>(::getpid());
#endif
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
    exec_or_throw(impl_->con, R"sql(
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
    if (!impl_->appender) impl_->appender.emplace(impl_->con, "buf");
    auto& app = *impl_->appender;
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
    // Not Close()d here -- close() flushes it once, before the COPY reads the
    // table. Closing per batch is what made the Appender per-batch in the first
    // place.
    impl_->n += static_cast<long long>(events.size());
}

void ParquetEventStore::close() {
    if (impl_->closed) return;
    impl_->closed = true;
    // Written even when empty: a day-file that yields no events must still
    // leave a file with the right schema, or a later read_parquet over the
    // directory fails because of it.
    //
    // Via a private name, then rename: the destination is derived from the
    // input's basename, so a re-dispatched work item has two processes writing
    // one path. Two concurrent COPY ... TO produce a torn file -- not the "two
    // differently-named files the reader dedups" the header claims, because
    // there is only one name. rename() is atomic within a filesystem and the
    // temp sits in the destination's own directory, so whichever writer lands
    // second replaces the first's file whole and every reader sees one
    // complete copy. Idempotency stays a property of the filename.
    // The Appender buffers; its rows are not in `buf` until it is closed, and
    // the COPY below reads `buf`. Flushing here rather than per write() is what
    // lets one Appender serve the store's whole life.
    if (impl_->appender) { impl_->appender->Close(); impl_->appender.reset(); }
    const std::string tmp = impl_->path + ".tmp." + std::to_string(process_id());
    exec_or_throw(impl_->con,
        "COPY (SELECT * FROM buf ORDER BY head_id, ts) TO '" +
        sql_quote(tmp) + "' (FORMAT PARQUET)");
    std::error_code ec;
    std::filesystem::rename(tmp, impl_->path, ec);
    if (ec) {
        // Never leave the temp behind: the reader globs *.parquet, which this
        // name deliberately does not match, but a stale one is still litter.
        std::error_code ignored;
        std::filesystem::remove(tmp, ignored);
        throw std::runtime_error("cannot rename " + tmp + " to " + impl_->path +
                                 ": " + ec.message());
    }
}

void ParquetEventStore::abandon() {
    impl_->closed = true;   // close() and ~ParquetEventStore now do nothing
    // And actually discard, which the header has always promised and this did
    // not do: the Appender is dropped without flushing, and the rows already in
    // `buf` go with the table. Behaviour did not depend on it -- nothing reads
    // the buffer after abandon() -- but a comment that overstates what the code
    // does is the kind of thing the next reader trusts.
    impl_->appender.reset();
    try {
        exec_or_throw(impl_->con, "DELETE FROM buf");
    } catch (const std::exception&) {
        // abandon() is called on failure paths and must not add one of its own.
        // The rows die with the connection either way.
    }
}

long long ParquetEventStore::count() const { return impl_->n; }

std::string parquet_path_for(const std::string& out_dir, const std::string& in_path) {
    return (std::filesystem::path(out_dir) /
            (std::filesystem::path(in_path).stem().string() + ".parquet")).string();
}

} // namespace mas
