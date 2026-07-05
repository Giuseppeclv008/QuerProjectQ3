#pragma once
#include "mas/CapEvent.hpp"
#include <fstream>
#include <string>

namespace mas {

// Streams a raw telemetry CSV: timestamp + 36 Count + 36 AppTorque + 36 Status.
// Discards the header line on construction.
class CsvRawReader {
public:
    explicit CsvRawReader(const std::string& path);
    bool next(RawRow& out);   // false at EOF

private:
    std::ifstream in_;
};

} // namespace mas
