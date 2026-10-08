#pragma once
#include "mas/domain/CapEvent.hpp"
#include "mas/domain/RawReader.hpp"
#include <cstddef>
#include <fstream>
#include <string>
#include <vector>

namespace mas {

// Streams a raw telemetry CSV: timestamp + 36 Count + 36 AppTorque + 36 Status.
// Validates the header line on construction.
// `final`, so the benchmark paths that hold a CsvRawReader by value call
// next() without a virtual dispatch.
class CsvRawReader final : public RawReader {
public:
    explicit CsvRawReader(const std::string& path);
    bool next(RawRow& out) override;   // false at EOF

    // False if the file could not be opened OR its header is not the expected
    // 109-column AROL layout. The header used to be read and thrown away, so a
    // pool delivered with a different column order was parsed as if it were this
    // one — counts read as torques, silently, with nothing downstream able to
    // tell. clean_file() reports such a file as unreadable (-1) instead.
    bool is_open() const override;
    std::size_t skipped() const override;               // rows dropped: short or malformed
    // Rows whose timestamp is <= the previous accepted row's (lexicographic;
    // the fixed ISO format makes that a time ordering). Detection, not
    // enforcement: the row is still returned -- an out-of-order row reads as
    // a counter reset downstream and a duplicate (head, ts) is absorbed by
    // the store's INSERT OR IGNORE, so this counter is the only place either
    // becomes visible. The identity key (spec: ts unique within a day-file)
    // depends on what it counts.
    std::size_t out_of_order() const override;
    const std::string& header_error() const override;   // empty when the header matched

    // "timestamp", then H01..H36 Count, H01..H36 AppTorque, H01..H36 Status.
    static std::vector<std::string> expected_header();

private:
    std::ifstream in_;
    std::size_t skipped_ = 0;
    std::size_t out_of_order_ = 0;
    std::string last_ts_;
    std::string header_error_;
};

} // namespace mas
