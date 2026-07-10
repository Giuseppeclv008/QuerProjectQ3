#pragma once
#include "mas/domain/CapEvent.hpp"
#include <cstddef>
#include <fstream>
#include <string>

namespace mas {

// Streams a raw telemetry CSV: timestamp + 36 Count + 36 AppTorque + 36 Status.
// Discards the header line on construction.
class CsvRawReader {
public:
    explicit CsvRawReader(const std::string& path);
    bool next(RawRow& out);   // false at EOF

    bool is_open() const;          // false if the file could not be opened
    std::size_t skipped() const;   // rows dropped: short or malformed numeric fields

private:
    std::ifstream in_;
    std::size_t skipped_ = 0;
};

} // namespace mas
