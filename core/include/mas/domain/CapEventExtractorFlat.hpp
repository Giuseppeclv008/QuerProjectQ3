#pragma once
#include "mas/domain/CapEvent.hpp"
#include <cstddef>
#include <string>
#include <vector>

namespace mas {

// Raw telemetry CSV loaded into column arrays. count/torque/status are
// row-major [n_rows][NUM_HEADS]; ts has one entry per row. `skipped` counts
// data rows dropped by the shared validity policy (RowParse.hpp) -- the same
// counter CsvRawReader::skipped() keeps, so a corrupt input cannot silently
// yield a short row set: the CUDA differential compares against the CPU on
// the same truncated data, and only this counter says the truncation happened.
struct RawColumns {
    std::vector<std::string> ts;
    std::vector<double> count, torque, status;
    std::size_t n_rows = 0;
    std::size_t skipped = 0;
};

// The 109 column names the pool uses (spec §4): "timestamp", then
// H01..H36 Count, H01..H36 AppTorque, H01..H36 Status.
std::vector<std::string> expected_header();

// Load `path` into `out`. Returns false and fills `error` if the file cannot be
// opened or its header is not expected_header(). Tolerates CRLF line endings.
bool load_columns(const std::string& path, RawColumns& out, std::string& error);

// Element-wise form of CapEventExtractor (spec §3): last_count_[h] after row i
// always equals llround(count[i][h]), so the transform never reads state older
// than the previous row. Row 0 is the seed and emits nothing. Appends to `out`
// in (row asc, head asc) order -- identical to CapEventExtractor's order.
void extract_flat(const std::vector<std::string>& ts,
                  const double* count, const double* torque, const double* status,
                  std::size_t n_rows, std::vector<CapEvent>& out);

} // namespace mas
