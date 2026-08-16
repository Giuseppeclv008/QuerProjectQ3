#include "mas/store/CsvRawReader.hpp"
#include "mas/domain/RowParse.hpp"
#include <string>
#include <vector>

namespace mas {

namespace {

std::string pad2(int n) { return (n < 10 ? "0" : "") + std::to_string(n); }

} // namespace

std::vector<std::string> CsvRawReader::expected_header() {
    std::vector<std::string> h;
    h.reserve(1 + NUM_HEADS * 3);
    h.push_back("timestamp");
    for (int i = 1; i <= NUM_HEADS; ++i) h.push_back("H" + pad2(i) + " Count");
    for (int i = 1; i <= NUM_HEADS; ++i) h.push_back("H" + pad2(i) + " AppTorque");
    for (int i = 1; i <= NUM_HEADS; ++i) h.push_back("H" + pad2(i) + " Status");
    return h;
}

CsvRawReader::CsvRawReader(const std::string& path) : in_(path) {
    if (!in_.is_open()) return;
    std::string header;
    if (!std::getline(in_, header)) {
        header_error_ = path + " is empty";
        return;
    }
    const auto want = expected_header();
    const auto got = split_csv_row(header);
    if (got.size() != want.size()) {
        header_error_ = path + ": header has " + std::to_string(got.size()) +
                        " columns, expected " + std::to_string(want.size());
        return;
    }
    for (std::size_t i = 0; i < want.size(); ++i) {
        if (got[i] != want[i]) {
            header_error_ = path + ": column " + std::to_string(i) + " is '" +
                            got[i] + "', expected '" + want[i] + "'";
            return;
        }
    }
}

bool CsvRawReader::is_open() const {
    return in_.is_open() && header_error_.empty();
}

std::size_t CsvRawReader::skipped() const { return skipped_; }

std::size_t CsvRawReader::out_of_order() const { return out_of_order_; }

const std::string& CsvRawReader::header_error() const { return header_error_; }

bool CsvRawReader::next(RawRow& out) {
    // Shared policy (RowParse.hpp): the reader used to keep its own splitter
    // that did NOT strip the trailing CR -- on the CRLF pool the last Status
    // cell reached std::stod as "0.0\r" and parsed only because stod
    // tolerates trailing garbage. load_columns stripped it; the two loaders
    // must not disagree on what a valid row is.
    std::string line;
    while (std::getline(in_, line)) {
        if (line.empty() || line == "\r") continue;
        if (parse_row_fields(split_csv_row(line), out) != RowParse::Ok) {
            ++skipped_;               // short, extra-field, malformed, or out-of-domain
            continue;
        }
        if (!last_ts_.empty() && out.ts <= last_ts_) ++out_of_order_;
        last_ts_ = out.ts;
        return true;
    }
    return false;
}

} // namespace mas
