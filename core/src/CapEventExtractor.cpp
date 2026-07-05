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
            CapEvent e;
            e.head_id = h + 1;
            e.ts = row.ts;
            e.cap_seq = c;
            e.delta = 1;                  // generalized in Task 2
            e.aggregated = false;         // generalized in Task 2
            e.is_fault = false;           // set in Task 3
            // app_torque, status: also set in Task 3 (left 0.0 until then)
            e.reset = false;
            out.push_back(e);
            last = c;
        }
        // held (c == *last): emit nothing
    }
}

} // namespace mas
