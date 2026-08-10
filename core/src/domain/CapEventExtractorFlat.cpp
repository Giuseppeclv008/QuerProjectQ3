#include "mas/domain/CapEventExtractorFlat.hpp"
#include <cmath>
#include <fstream>
#include <sstream>

namespace mas {

namespace {

std::string pad2(int n) {
    return (n < 10 ? "0" : "") + std::to_string(n);
}

// Split on ',' and drop a trailing CR. Kept local: load_columns is the only
// caller, and CsvRawReader has its own splitter with different error semantics.
std::vector<std::string> splitLine(const std::string& line) {
    std::vector<std::string> f;
    f.reserve(1 + NUM_HEADS * 3);
    std::string cur;
    std::istringstream ss(line);
    while (std::getline(ss, cur, ',')) {
        if (!cur.empty() && cur.back() == '\r') cur.pop_back();
        f.push_back(cur);
    }
    return f;
}

} // namespace

std::vector<std::string> expected_header() {
    std::vector<std::string> h;
    h.reserve(1 + NUM_HEADS * 3);
    h.push_back("timestamp");
    for (int i = 1; i <= NUM_HEADS; ++i) h.push_back("H" + pad2(i) + " Count");
    for (int i = 1; i <= NUM_HEADS; ++i) h.push_back("H" + pad2(i) + " AppTorque");
    for (int i = 1; i <= NUM_HEADS; ++i) h.push_back("H" + pad2(i) + " Status");
    return h;
}

bool load_columns(const std::string& path, RawColumns& out, std::string& error) {
    std::ifstream in(path, std::ios::binary);
    if (!in.is_open()) { error = "cannot open " + path; return false; }

    std::string line;
    if (!std::getline(in, line)) { error = path + " is empty"; return false; }
    const auto want = expected_header();
    const auto got = splitLine(line);
    if (got.size() != want.size()) {
        error = path + ": header has " + std::to_string(got.size()) +
                " columns, expected " + std::to_string(want.size());
        return false;
    }
    for (std::size_t i = 0; i < want.size(); ++i) {
        if (got[i] != want[i]) {
            error = path + ": column " + std::to_string(i) + " is '" + got[i] +
                    "', expected '" + want[i] + "'";
            return false;
        }
    }

    out = RawColumns{};
    while (std::getline(in, line)) {
        if (line.empty() || line == "\r") continue;
        const auto f = splitLine(line);
        if (f.size() < static_cast<std::size_t>(1 + NUM_HEADS * 3)) continue;
        try {
            out.ts.push_back(f[0]);
            for (int h = 0; h < NUM_HEADS; ++h) out.count.push_back(std::stod(f[1 + h]));
            for (int h = 0; h < NUM_HEADS; ++h) out.torque.push_back(std::stod(f[1 + NUM_HEADS + h]));
            for (int h = 0; h < NUM_HEADS; ++h) out.status.push_back(std::stod(f[1 + 2 * NUM_HEADS + h]));
        } catch (const std::exception&) {
            out.ts.resize(out.n_rows);                    // roll the partial row back
            out.count.resize(out.n_rows * NUM_HEADS);
            out.torque.resize(out.n_rows * NUM_HEADS);
            out.status.resize(out.n_rows * NUM_HEADS);
            continue;
        }
        ++out.n_rows;
    }
    return true;
}

void extract_flat(const std::vector<std::string>& ts,
                  const double* count, const double* torque, const double* status,
                  std::size_t n_rows, std::vector<CapEvent>& out) {
    for (std::size_t i = 1; i < n_rows; ++i) {
        const std::size_t cur = i * NUM_HEADS;
        const std::size_t prv = (i - 1) * NUM_HEADS;
        for (int h = 0; h < NUM_HEADS; ++h) {
            const long long c_cur = std::llround(count[cur + h]);
            const long long c_prv = std::llround(count[prv + h]);
            if (c_cur == c_prv) continue;          // held: emit nothing

            CapEvent e;
            e.head_id = h + 1;
            e.ts = ts[i];
            e.cap_seq = c_cur;
            e.app_torque = torque[cur + h];
            e.status = status[cur + h];
            e.delta = (c_cur > c_prv) ? static_cast<int>(c_cur - c_prv) : 0;
            e.is_fault = is_reject(status[cur + h]);
            e.aggregated = e.delta > 1;
            e.reset = c_cur < c_prv;
            out.push_back(e);
        }
    }
}

} // namespace mas
