#include "mas/store/CsvRawReader.hpp"
#include <sstream>
#include <string>
#include <vector>

namespace mas {

namespace {

std::string pad2(int n) { return (n < 10 ? "0" : "") + std::to_string(n); }

std::vector<std::string> splitCsv(const std::string& line) {
    std::vector<std::string> f;
    std::string cur;
    std::istringstream ss(line);
    while (std::getline(ss, cur, ',')) {
        if (!cur.empty() && cur.back() == '\r') cur.pop_back();
        f.push_back(cur);
    }
    return f;
}

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
    const auto got = splitCsv(header);
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

const std::string& CsvRawReader::header_error() const { return header_error_; }

bool CsvRawReader::next(RawRow& out) {
    std::string line;
    while (std::getline(in_, line)) {
        if (line.empty()) continue;

        std::vector<std::string> f;
        f.reserve(1 + NUM_HEADS * 3);
        std::string cur;
        std::istringstream ss(line);
        while (std::getline(ss, cur, ',')) f.push_back(cur);
        if (f.size() < static_cast<size_t>(1 + NUM_HEADS * 3)) {
            ++skipped_;               // truncated/corrupt line
            continue;
        }

        try {
            out.ts = f[0];
            for (int h = 0; h < NUM_HEADS; ++h) {
                out.count[h]  = std::stod(f[1 + h]);
                out.torque[h] = std::stod(f[1 + NUM_HEADS + h]);
                out.status[h] = std::stod(f[1 + 2 * NUM_HEADS + h]);
            }
        } catch (const std::exception&) {
            ++skipped_;               // malformed numeric cell
            continue;
        }
        return true;
    }
    return false;
}

} // namespace mas
