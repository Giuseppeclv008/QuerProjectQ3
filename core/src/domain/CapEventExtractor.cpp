#include "mas/domain/CapEventExtractor.hpp"
#include <climits>
#include <cmath>

namespace mas {

namespace {
mas::CapEvent makeEvent(const mas::RawRow& row, int h, long long c, int delta, bool reset) {
    mas::CapEvent e;
    e.head_id = h + 1;
    e.ts = row.ts;
    e.cap_seq = c;
    e.app_torque = row.torque[h];
    e.status = row.status[h];
    e.delta = delta;
    e.is_fault = mas::is_reject(row.status[h]);
    e.aggregated = delta > 1;
    e.reset = reset;
    return e;
}
} // namespace

void CapEventExtractor::process(const RawRow& row, std::vector<CapEvent>& out) {
    for (int h = 0; h < NUM_HEADS; ++h) {
        const long long c = std::llround(row.count[h]);
        auto& last = last_count_[h];
        if (!last.has_value()) {          // first observation: seed, no event
            last = c;
            continue;
        }
        if (c > *last) {                  // real cap applied
            // Same saturation as the flat extractor: an over-int jump must
            // not truncate into a small delta (and never into <= 1, which
            // would also clear `aggregated`).
            const long long jump = c - *last;
            out.push_back(makeEvent(
                row, h, c,
                static_cast<int>(jump > INT_MAX ? INT_MAX : jump), false));
            last = c;
        }
        else if (c < *last) {             // counter reset / rollover
            out.push_back(makeEvent(row, h, c, 0, true));
            last = c;
        }
        // held (c == *last): emit nothing
    }
}

} // namespace mas
