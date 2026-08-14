#pragma once
#include <string>

namespace mas {

// Result of an export: how many rows were written, and where.
struct ExportResult {
    long long rows = 0;
    std::string path;
};

// Export an existing DuckDB store's cap_events to a Parquet file.
//
// DuckDB stays the persistent format. This exists so a store can be handed to
// something that does not speak .duckdb -- another tool, another language,
// cold archive -- at roughly a fifth of the bytes.
//
// Opens the store READ-ONLY. That is the point of not routing this through
// DuckDbEventStore, whose constructor creates tables and writes store_meta:
// exporting must not modify what it exports, and must work on a store that is
// read-only on disk. COPY ... TO writes the destination file, not the database.
//
// since/until, when non-empty, bound ts inclusively (any literal DuckDB parses
// as a TIMESTAMP, e.g. "2026-02-03" or "2026-02-03 12:00:00"). An empty string
// means unbounded on that side.
//
// A date with no time as the UPPER bound means the end of that day, not its
// midnight: "--until 2026-02-03" includes the whole 3rd. Read literally it
// would exclude the entire day while still succeeding and still reporting a
// count, which is the kind of quiet wrong answer this codebase treats as a
// defect. An explicit time is honoured exactly as written. A range that matches nothing is not an error:
// it writes a valid empty Parquet carrying the schema, so a later
// read_parquet() over a glob including it still works.
//
// Rows come out ordered by (head_id, ts) so the file is deterministic --
// two exports of the same store are byte-comparable.
//
// After writing, the file is read back and its row count compared with the
// count the same predicate returns from the store. A mismatch throws rather
// than leaving a plausible, wrong file on disk.
//
// Refuses to write a destination that already exists, and refuses outright when
// the destination IS the store: COPY ... TO truncates its target, so exporting
// a store onto itself replaced the database with the Parquet of its own
// contents and then verified that Parquet against itself and exited 0.
//
// Throws std::runtime_error if the store cannot be opened read-only, if the
// destination exists, if the COPY fails, or if the verification does not
// match.
ExportResult export_store_to_parquet(const std::string& db_path,
                                     const std::string& out_path,
                                     const std::string& since = "",
                                     const std::string& until = "");

} // namespace mas
