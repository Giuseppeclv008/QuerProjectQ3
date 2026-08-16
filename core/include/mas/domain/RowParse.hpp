#pragma once
#include "mas/domain/CapEvent.hpp"
#include <string>
#include <vector>

namespace mas {

// One row-validity policy for both loaders. CsvRawReader (every non-CUDA
// path) and load_columns (the CUDA path) grew as near-duplicates and drifted:
// the reader did not strip the trailing CR from the last field (the real pool
// is CRLF, so "0.0\r" reached stod and parsed only because stod tolerates
// trailing garbage), they disagreed on whether a row with extra fields is
// acceptable, and only Count cells were range-checked -- a torque of 1e300
// narrowed to float inf in both persistent stores, which is UB on the cast
// and poison for every AVG/SUM/MAX downstream. One splitter and one parser
// make those disagreements impossible.

enum class RowParse {
    Ok,
    Short,       // fewer than 1 + 3*NUM_HEADS fields
    Extra,       // more than expected: trailing garbage, not a known layout
    BadNumeric,  // a cell stod cannot parse at all
    BadCount,    // a Count outside ±2^53 (is_valid_count)
    BadReal,     // a torque/status outside float range or NaN (is_valid_real)
};

// Split on ',', dropping one trailing CR per cell (CRLF pool read in binary).
std::vector<std::string> split_csv_row(const std::string& line);

// Parse an already-split data row into ts + 3*NUM_HEADS doubles, applying the
// full validity policy. On anything but Ok, `out` must not be used.
RowParse parse_row_fields(const std::vector<std::string>& f, RawRow& out);

} // namespace mas
