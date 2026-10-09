#pragma once
#include "mas/domain/RawReader.hpp"
#include <cstddef>
#include <memory>
#include <string>

namespace mas {

// Raw input formats the pipeline ingests (brief §3.1: "CSV / JSON /
// Parquet"). The format is the file's extension, case-insensitive:
// .parquet -> Parquet; .json, .jsonl, .ndjson -> Json; anything else is CSV,
// which is what every input was before the other two existed.
enum class RawFormat { Csv, Parquet, Json };

RawFormat raw_format_for(const std::string& path);
const char* raw_format_name(RawFormat f);

// Parquet and JSON day-files, read through DuckDB. Columns are matched by
// NAME, not position -- both formats carry names with every value, so a file
// whose columns are in another order is still unambiguous. The set must still
// be exactly the 109 AROL columns: a missing one cannot be cleaned, and an
// extra one is a layout nobody has looked at (the CSV reader refuses both for
// the same reason).
//
// Every cell reaches parse_row_fields() as text, so the validity policy is the
// CSV one: a NULL, non-numeric or out-of-range cell skips the row and counts in
// skipped(), it does not fail the file. JSON is read with every column as
// VARCHAR for exactly that reason -- with inferred types, one "abc" in a
// numeric column would abort the whole read instead of costing one row.
// A timestamp is formatted back to the pool's "2026-02-27T16:00:00.000"
// whether the file typed it (Parquet TIMESTAMP) or spelled it another way, so
// the same day yields identical events whichever format it arrived in.
class TabularRawReader final : public RawReader {
public:
    TabularRawReader(const std::string& path, RawFormat format);
    ~TabularRawReader() override;
    TabularRawReader(const TabularRawReader&) = delete;
    TabularRawReader& operator=(const TabularRawReader&) = delete;

    // Throws std::runtime_error if DuckDB fails mid-file (a corrupt row
    // group); a file that cannot be opened at all is is_open() == false.
    bool next(RawRow& out) override;
    bool is_open() const override;
    std::size_t skipped() const override;
    std::size_t out_of_order() const override;
    const std::string& header_error() const override;

private:
    struct Impl;   // DuckDB stays out of this header (mas_store links it PRIVATE)
    std::unique_ptr<Impl> impl_;
};

// The reader for `path`'s format. Never null; check is_open().
std::unique_ptr<RawReader> open_raw_reader(const std::string& path);

} // namespace mas
