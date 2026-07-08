#pragma once
#include "mas/EventStore.hpp"
#include <memory>
#include <string>

namespace mas {

// DuckDB-backed cap_events store (spec §6 schema + is_reset column).
// Idempotent: UNIQUE(machine_id, head_id, cap_seq) + INSERT OR IGNORE,
// so reprocessing any day-file is safe (spec §10). Single-writer only
// (multi-process concurrency is spec §14 Q4, a later plan).
class DuckDbEventStore : public IEventStore {
public:
    // Opens/creates the database and the cap_events table.
    // Throws std::runtime_error if the database cannot be opened.
    DuckDbEventStore(const std::string& db_path, const std::string& machine_id);
    ~DuckDbEventStore() override;

    void write(std::span<const CapEvent> events) override;
    long long count() const;                             // rows in cap_events
    void export_parquet(const std::string& parquet_path); // implemented in Task 5

    // Idempotently union another store file's cap_events into this one
    // (ATTACH read-only + INSERT OR IGNORE). Sink-side answer to spec §14
    // Q4: workers write per-worker stores, never one file concurrently.
    // other_db_path must be a closed/checkpointed store file: ATTACH ...
    // (READ_ONLY) reads only the on-disk database file and may not observe
    // rows still sitting in an unflushed WAL of a store that is still open.
    // Precondition: the source store must be closed/checkpointed before the
    // call — ATTACH READ_ONLY may not see another connection's unflushed WAL.
    void merge_from(const std::string& other_db_path);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace mas
