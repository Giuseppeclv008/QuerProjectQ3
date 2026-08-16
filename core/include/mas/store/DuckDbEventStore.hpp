#pragma once
#include "mas/store/EventStore.hpp"
#include <memory>
#include <string>
#include <vector>

namespace mas {

// DuckDB-backed cap_events store (spec §6 schema + is_reset column).
// Idempotent: UNIQUE(machine_id, head_id, ts) + INSERT OR IGNORE,
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
    // (There is deliberately no export_parquet() here: COPY ... TO truncates
    // its destination, so the unguarded member form could destroy the store
    // it was reading -- `store.export_parquet(db_path)` did exactly that.
    // Export goes through export_store_to_parquet(), which opens READ_ONLY
    // and carries the self/WAL-overwrite guards and count verification.)

    // Idempotently union another store file's cap_events into this one
    // (ATTACH read-only + INSERT OR IGNORE). Sink-side answer to spec §14
    // Q4: workers write per-worker stores, never one file concurrently.
    // other_db_path must be a closed/checkpointed store file: ATTACH ...
    // (READ_ONLY) reads only the on-disk database file and may not observe
    // rows still sitting in an unflushed WAL of a store that is still open.
    // Precondition: the source store must be closed/checkpointed before the
    // call — ATTACH READ_ONLY may not see another connection's unflushed WAL.
    void merge_from(const std::string& other_db_path);

    // Merge many source stores in one statement.
    //
    // merge_from() called N times does N sequential INSERT OR IGNORE passes,
    // each probing the UNIQUE index once per row against a destination that
    // keeps growing. At month scale that is ~22M index probes to find, almost
    // always, nothing: day-files are contiguous and non-overlapping, so two
    // different files cannot produce the same (machine_id, head_id, ts) and the
    // sources are disjoint by construction.
    //
    // Almost always, not always: a worker declared dead while still working has
    // its file re-dispatched, so the same file can land in two stores. Those
    // rows are byte-identical (same input, same extraction), so one hash-based
    // DISTINCT over the union settles it in a single pass instead of per-row.
    //
    // Same preconditions as merge_from: every source must be closed and
    // checkpointed. Falls back to repeated merge_from when the destination
    // already holds rows, which the bulk path is not designed for.
    void merge_all(const std::vector<std::string>& other_db_paths);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace mas
