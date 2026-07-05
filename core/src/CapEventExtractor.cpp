#include "mas/CapEventExtractor.hpp"
#include <cmath>

namespace mas {

void CapEventExtractor::process(const RawRow& row, std::vector<CapEvent>& out) {
    for (int h = 0; h < NUM_HEADS; ++h) {
        const long long c = std::llround(row.count[h]);
        auto& last = last_count_[h];
        if (!last.has_value()) {          // first observation: seed, no event
            last = c;
            continue;
        }
        if (c > *last) {                  // real cap applied
            const int delta = static_cast<int>(c - *last);
            CapEvent e;
            e.head_id = h + 1;
            e.ts = row.ts;
            e.cap_seq = c;
            e.delta = delta;
            e.aggregated = delta > 1;
            e.app_torque = row.torque[h];
            e.status = row.status[h];
            e.is_fault = is_fault_status(row.status[h]);
            e.reset = false;
            out.push_back(e);
            last = c;
        }
        // held (c == *last): emit nothing
    }
}

} // namespace mas
