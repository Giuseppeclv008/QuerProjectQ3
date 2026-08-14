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
// reprocessing an input writes the same path and replaces it. Duplicates from
// a re-dispatched work item land in two differently-named files and are
// removed by the reader's view (spec §3).
class ParquetEventStore : public IEventStore {
public:
    // Throws std::runtime_error if the parent directory does not exist and
    // cannot be created.
    ParquetEventStore(const std::string& out_path, const std::string& machine_id);
    ~ParquetEventStore() override;   // calls close(), logs and swallows failure

    void write(std::span<const CapEvent> events) override;   // buffers
    void close();                    // writes the file; throws on failure

    // Discard the buffer and write nothing, ever -- close() and the destructor
    // both become no-ops. For a day-file whose clean failed: without this the
    // destructor still wrote a valid, empty Parquet, and the reader's glob
    // cannot tell that file from a day that legitimately produced no events.
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
