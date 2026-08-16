#include "mas/domain/RowParse.hpp"
#include <sstream>
#include <stdexcept>

namespace mas {

std::vector<std::string> split_csv_row(const std::string& line) {
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

RowParse parse_row_fields(const std::vector<std::string>& f, RawRow& out) {
    constexpr std::size_t want = 1 + NUM_HEADS * 3;
    if (f.size() < want) return RowParse::Short;
    // A row with extra fields is not "this layout plus junk" -- it is a layout
    // this parser does not know, and silently taking the first 109 cells
    // risks reading a shifted column. The header check is exact; rows now are
    // too.
    if (f.size() > want) return RowParse::Extra;

    try {
        out.ts = f[0];
        for (int h = 0; h < NUM_HEADS; ++h) {
            out.count[h]  = std::stod(f[1 + h]);
            out.torque[h] = std::stod(f[1 + NUM_HEADS + h]);
            out.status[h] = std::stod(f[1 + 2 * NUM_HEADS + h]);
        }
    } catch (const std::exception&) {
        return RowParse::BadNumeric;
    }
    for (int h = 0; h < NUM_HEADS; ++h)
        if (!is_valid_count(out.count[h])) return RowParse::BadCount;
    // stod accepts "1e300", "inf" and "nan" for torque/status too, and both
    // persistent stores narrow those columns to float -- validate all three
    // columns with one policy, not just Count.
    for (int h = 0; h < NUM_HEADS; ++h)
        if (!is_valid_real(out.torque[h]) || !is_valid_real(out.status[h]))
            return RowParse::BadReal;
    return RowParse::Ok;
}

} // namespace mas
