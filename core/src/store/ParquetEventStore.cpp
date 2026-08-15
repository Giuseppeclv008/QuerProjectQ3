#include "mas/store/ParquetEventStore.hpp"
#include "mas/store/DuckDbExec.hpp"
#include "mas/store/SqlQuote.hpp"
#include <duckdb.hpp>
#include <atomic>
#include <filesystem>
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
    bool failed = false;        // a write() threw; this store must write nothing
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
    // Cleanup only. Publishing a file is an explicit act -- close() -- and this
    // is not it.
    //
    // The destructor used to call close(), which made every escape between the
    // first write() and the close() a publication: the rows accepted so far
    // became a file in the directory the reader globs, with nothing to mark it
    // as partial. A failure inside write() is only the nearest such escape. The
    // one that actually happens is a throw *between* writes -- BeatingStore
    // calls inner_.write() and then beat_(), and beat_() reaches
    // ZmqPushSink::send, which throws when the coordinator stops draining, the
    // exact case the resilience design is built around. No latch inside this
    // class can see that, and the count check cannot either: `n` and `buf`
    // agree perfectly, because both describe the same truncated set.
    //
    // So the rule is the one that does not depend on enumerating the ways a
    // clean can fail: an unfinished store writes nothing. All four call sites
    // close() on their success path, and abandon() stays for the failure they
    // detect themselves (a clean that returns < 0 without throwing).
    if (impl_->appender) {
        // Dropping it flushes into buf -- DuckDB's destructor calls Close()
        // unless an exception is unwinding -- which is harmless here only
        // because nothing reads buf afterwards. Cleared anyway, so that stays
        // true if this ever grows a caller.
        impl_->appender.reset();
        try {
            exec_or_throw(impl_->con, "DELETE FROM buf");
        } catch (const std::exception&) {
            // A destructor cannot report failure and must not throw. The rows
            // die with the connection when impl_ is destroyed regardless.
        }
    }
}

void ParquetEventStore::write(std::span<const CapEvent> events) {
    // A store that has published is finished. Without this, write() accepted
    // rows it would never write while count() went on reporting them -- silent
    // loss, with the accessor confirming that no loss had occurred. Checked
    // before the empty-span shortcut, so a published store rejects every write
    // rather than only the non-empty ones.
    //
    // `failed` deliberately gets no matching check, so that asymmetry stays:
    // write({}) on a failed store returns quietly, and a non-empty one is
    // refused only incidentally, because DuckDB's Appender is stuck mid-row --
    // the next row's machine_id lands in the still-open ts column and fails the
    // cast there. Nothing rests on either: `failed` is latched and close()
    // refuses on it.
    if (impl_->closed)
        throw std::runtime_error("write() after close() on " + impl_->path);
    if (events.empty()) return;
    auto& app = [&]() -> duckdb::Appender& {
        // Inside the latch's reach: if constructing the Appender threw, `failed`
        // stayed clear and the old destructor wrote a valid, empty Parquet.
        try {
            if (!impl_->appender) impl_->appender.emplace(impl_->con, "buf");
        } catch (...) {
            impl_->failed = true;
            throw;
        }
        return *impl_->appender;
    }();
    try {
        for (const auto& e : events) {
            app.BeginRow();
            app.Append(duckdb::Value(impl_->machine_id));
            app.Append(duckdb::Value::SMALLINT(static_cast<int16_t>(e.head_id)));
            app.Append(duckdb::Value(e.ts));          // VARCHAR -> TIMESTAMP cast
            app.Append(duckdb::Value::BIGINT(e.cap_seq));
            app.Append(duckdb::Value::FLOAT(static_cast<float>(e.app_torque)));
            app.Append(duckdb::Value::FLOAT(static_cast<float>(e.status)));
            app.Append(duckdb::Value::INTEGER(e.delta));
            app.Append(duckdb::Value::BOOLEAN(e.is_fault));
            app.Append(duckdb::Value::BOOLEAN(e.aggregated));
            app.Append(duckdb::Value::BOOLEAN(e.reset));
            app.EndRow();
        }
    } catch (...) {
        // A row this store could not accept means the file it would write is
        // not the day it was asked for, so the store is finished: close()
        // refuses and the destructor stays quiet. Without the latch the throw
        // propagated past a store whose destructor then wrote whatever had
        // already been flushed -- a short file, reported as a real day.
        //
        // The cast on `ts` is the reachable case: CsvRawReader copies that cell
        // verbatim (only the numeric cells go through stod and the skipped_
        // counter), so a corrupt timestamp arrives here intact. The Appender is
        // also stuck mid-row at this point -- DuckDB's Close() only flushes on a
        // row boundary -- which is why it cannot simply be flushed and kept.
        impl_->failed = true;
        throw;
    }
    // Not Close()d here -- close() flushes it once, before the COPY reads the
    // table. Closing per batch is what made the Appender per-batch in the first
    // place.
    impl_->n += static_cast<long long>(events.size());
}

void ParquetEventStore::close() {
    if (impl_->closed) return;
    impl_->closed = true;
    // A store that failed mid-write never produces a file. Same reasoning as
    // abandon(), reached without the caller having to remember: the reader
    // globs this directory, and a file holding part of a day is worse than no
    // file, because nothing downstream can tell it is partial.
    if (impl_->failed) {
        impl_->appender.reset();
        throw std::runtime_error("refusing to write " + impl_->path +
                                 ": a write failed, so this file would be a "
                                 "partial day presented as a whole one");
    }
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

    // What the store accepted must equal what the table holds. This compares
    // the store against itself, so be clear about its reach: it catches a flush
    // that did not happen, a row DuckDB dropped, a future change to the
    // buffering -- and it cannot see a short *input*, because there `n` and
    // `buf` agree perfectly on the same truncated set. That case is the
    // destructor's to handle, by never publishing. ParquetExport verifies its
    // own output in the same spirit.
    const long long in_table = scalar_or_throw(impl_->con, "SELECT COUNT(*) FROM buf");
    if (in_table != impl_->n)
        throw std::runtime_error(
            "refusing to write " + impl_->path + ": buffered " +
            std::to_string(impl_->n) + " events but the table holds " +
            std::to_string(in_table));

    // Unique per store, not just per process: two stores on one path inside one
    // process would otherwise share a temp name and truly tear it. No caller
    // does that today -- monolith-MT gives each thread its own file, a worker
    // handles one item at a time -- but the whole point of this dance is that
    // the collision case is the one nobody arranges deliberately. A counter
    // rather than `this`: an object address is equally unique among live
    // objects, and puts a memory address into a filename and into the error
    // text a user reads.
    static std::atomic<unsigned long long> next_token{0};
    const std::string tmp = impl_->path + ".tmp." + std::to_string(process_id()) +
                            "." + std::to_string(next_token.fetch_add(1));
    try {
        exec_or_throw(impl_->con,
            "COPY (SELECT * FROM buf ORDER BY head_id, ts) TO '" +
            sql_quote(tmp) + "' (FORMAT PARQUET)");
        std::error_code ec;
        std::filesystem::rename(tmp, impl_->path, ec);
        if (ec)
            throw std::runtime_error("cannot rename " + tmp + " to " + impl_->path +
                                     ": " + ec.message());
    } catch (...) {
        // Never leave the temp behind, on any failure and not just a failed
        // rename: the reader globs *.parquet, which this name deliberately does
        // not match, but a partial file from an out-of-space COPY is still
        // litter in a directory somebody has to reason about.
        std::error_code ignored;
        std::filesystem::remove(tmp, ignored);
        throw;
    }
}

void ParquetEventStore::abandon() {
    impl_->closed = true;   // a close() reached later has nothing left to do
    // And actually discard, which the header has always promised and this did
    // not do. Order matters here, and not in the direction the previous comment
    // claimed: dropping the Appender does NOT throw its buffered rows away.
    // DuckDB's destructor calls Close() unless an exception is unwinding, so
    // reset() flushes them into `buf` -- the DELETE below is what removes them,
    // and swapping these two lines would silently leave a buffer behind.
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
