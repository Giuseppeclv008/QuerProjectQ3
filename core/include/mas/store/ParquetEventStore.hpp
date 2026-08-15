#pragma once
#include "mas/store/EventStore.hpp"
#include <memory>
#include <span>
#include <string>

namespace mas {

// One Parquet file per input day-file: no index, no write-ahead log, no
// constraint. That is the point — this store exists so the project can measure
// what those cost in DuckDbEventStore, where persistence is 79.8% of
// end-to-end wall-clock (docs/bench/results.md).
//
// Idempotency is a property of the filename rather than of a UNIQUE key:
// reprocessing an input writes the same path and replaces it. Note what this
// does NOT mean, because the spec's §3 wording invites the wrong reading: a
// re-dispatched work item does not land in two differently-named files here.
// The name comes from the input, so both writers target one path -- and it is
// close()'s write-to-temp-then-rename that makes the result one whole file
// instead of a torn one. The reader's dedup covers duplicate *rows* across
// different inputs; it cannot repair a half-written file.
class ParquetEventStore : public IEventStore {
public:
    // Throws std::runtime_error if the parent directory does not exist and
    // cannot be created.
    ParquetEventStore(const std::string& out_path, const std::string& machine_id);
    // Discards the buffer. Writes nothing, ever -- publishing is close()'s job
    // and only close()'s. A store destroyed without it leaves no file, which is
    // what makes "an unfinished clean produces nothing" hold for every way a
    // clean can be interrupted rather than only the ones this class can see.
    ~ParquetEventStore() override;

    void write(std::span<const CapEvent> events) override;   // buffers
    // Writes the file. Throws on failure, and on the first call after a failed
    // write() -- a second call after that one returns quietly, having nothing
    // left to refuse.
    void close();

    // Discard the buffer now, and disarm a later close(). Call it on a clean
    // that failed without throwing -- one that returns a negative count -- so
    // the intent is in the call site rather than inferred from a scope exit.
    //
    // Note what it no longer carries: it used to be what stopped the destructor
    // from writing a valid, empty Parquet that the reader's glob could not tell
    // from a day with no events. The destructor writes nothing at all now, so
    // omitting this call would also produce no file. What it still buys is the
    // explicit statement and one guard: a close() reached later -- from a
    // catch-all somebody adds, say -- finds the store already spent.
    //
    // The DuckDB backend has no equivalent hazard: a failed run simply lacks
    // the rows, and count() shows the shortfall.
    void abandon();

    long long count() const;         // events accepted so far

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

// <dir>/<input basename without extension>.parquet — the naming that makes
// reprocessing idempotent.
std::string parquet_path_for(const std::string& out_dir, const std::string& in_path);

} // namespace mas
