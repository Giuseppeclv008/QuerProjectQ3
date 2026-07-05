#include "mas/CsvRawReader.hpp"
#include <sstream>
#include <string>
#include <vector>

namespace mas {

CsvRawReader::CsvRawReader(const std::string& path) : in_(path) {
    std::string header;
    std::getline(in_, header);   // discard header
}

bool CsvRawReader::next(RawRow& out) {
    std::string line;
    while (std::getline(in_, line)) {
        if (line.empty()) continue;

        std::vector<std::string> f;
        f.reserve(1 + NUM_HEADS * 3);
        std::string cur;
        std::istringstream ss(line);
        while (std::getline(ss, cur, ',')) f.push_back(cur);
        if (f.size() < static_cast<size_t>(1 + NUM_HEADS * 3)) continue;

        out.ts = f[0];
        for (int h = 0; h < NUM_HEADS; ++h) {
            out.count[h]  = std::stod(f[1 + h]);
            out.torque[h] = std::stod(f[1 + NUM_HEADS + h]);
            out.status[h] = std::stod(f[1 + 2 * NUM_HEADS + h]);
        }
        return true;
    }
    return false;
}

} // namespace mas
