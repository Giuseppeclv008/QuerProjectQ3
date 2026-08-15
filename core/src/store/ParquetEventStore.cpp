#include "mas/store/ParquetEventStore.hpp"
#include "mas/store/AtomicPublish.hpp"
#include "mas/store/DuckDbExec.hpp"
#include "mas/store/SqlQuote.hpp"
#include <duckdb.hpp>
#include <filesystem>
#include <optional>
#include <stdexcept>

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

ParquetEventStore::ParquetEventStore(const std::string& out_path,
                                     const std::string& machine_id) {
    const auto parent = std::filesystem::path(out_path).parent_path();
    if (!parent.empty()) {
        // Non-throwing is_directory, and this is the reachable one of the pair:
        // ParquetExport's identical guard sits behind a stat that has already
        // refused an unreadable prefix by name, but nothing pre-checks here. A
        // parent under a chmod-400 directory leaked
        // "filesystem error: in posix_stat: ... Permission denied" -- the raw
        // message this class's own error text exists to replace, and it fired
        // instead of "cannot create directory <p>: <reason>", never letting
        // this guard say what it was written to say.
        std::error_code mk_ec, dir_ec;
        std::filesystem::create_directories(parent, mk_ec);
        if (mk_ec && !std::filesystem::is_directory(parent, dir_ec))
            throw std::runtime_error("cannot create directory " + parent.string() +
                                     ": " + mk_ec.message());
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
        throw std::runtime_error(
            "write() after close() or abandon() on " + impl_->path);
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
    // Latched before anything can fail, which makes close() one-shot rather
    // than retryable: if the publish below throws, a second close() returns
    // quietly having written nothing, reporting success it did not achieve. No
    // caller retries -- all four treat a throw here as fatal to that day-file --
    // and the alternative is worse, since clearing `closed` on failure would
    // let a caller retry a publish whose temp is already gone. Stated because
    // the header promises "throws on failure" and this is the shape of the
    // exception to that.
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
    //
    // No test reaches this, and that is a statement about the check rather than
    // a gap to fill: every public route to a disagreement sets `failed` first
    // and is refused above. It is a backstop against a future change to the
    // buffering, so the only thing that could exercise it is the bug it exists
    // to catch. Said here because a test asserting only the exception type
    // *appeared* to cover it -- both refusals throw runtime_error, so deleting
    // the latch above left the suite green with this check standing in for it.
    // The tests now assert on the message for that reason.
    const long long in_table = scalar_or_throw(impl_->con, "SELECT COUNT(*) FROM buf");
    if (in_table != impl_->n)
        throw std::runtime_error(
            "refusing to write " + impl_->path + ": buffered " +
            std::to_string(impl_->n) + " events but the table holds " +
            std::to_string(in_table));

    // The temp name, the rename and the remove-on-throw all live in
    // publish_atomically now, shared with export_store_to_parquet -- which had
    // none of it, and left truncated Parquet under the user's chosen name when
    // a COPY ran out of space. One writer having the discipline and the other
    // not is the kind of asymmetry a shared helper makes hard to reintroduce.
    publish_atomically(impl_->path, [&](const std::string& tmp) {
        exec_or_throw(impl_->con,
            "COPY (SELECT * FROM buf ORDER BY head_id, ts) TO '" +
            sql_quote(tmp) + "' (FORMAT PARQUET)");
    });
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

// Events accepted, which after abandon() is not events written -- the rows are
// gone and this still reports them. Left that way deliberately: no caller reads
// count() on a path that abandoned, and the alternative (zeroing it) would make
// the accessor mean two different things depending on how the store ended.
long long ParquetEventStore::count() const { return impl_->n; }

std::string parquet_path_for(const std::string& out_dir, const std::string& in_path) {
    return (std::filesystem::path(out_dir) /
            (std::filesystem::path(in_path).stem().string() + ".parquet")).string();
}

} // namespace mas
