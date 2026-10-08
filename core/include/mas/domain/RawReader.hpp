#pragma once
#include "mas/domain/CapEvent.hpp"
#include <cstddef>
#include <string>

namespace mas {

// One raw telemetry source, whatever the file format: the 109 AROL columns
// (timestamp, H01..H36 Count, AppTorque, Status) delivered row by row in file
// order. CsvRawReader (mas_clean_core, no DuckDB) and TabularRawReader
// (mas_store: Parquet and JSON through DuckDB) implement it; open_raw_reader()
// in mas/store/RawInput.hpp picks one by extension. Every implementation runs
// its rows through parse_row_fields(), so a valid row means the same thing in
// every format.
class RawReader {
public:
    virtual ~RawReader() = default;
    virtual bool next(RawRow& out) = 0;   // false at end of input

    // False if the file could not be opened OR its columns are not the AROL
    // layout; header_error() then says which.
    virtual bool is_open() const = 0;
    virtual std::size_t skipped() const = 0;        // rows dropped by the policy
    virtual std::size_t out_of_order() const = 0;   // ts <= previous row's
    virtual const std::string& header_error() const = 0;
};

} // namespace mas
