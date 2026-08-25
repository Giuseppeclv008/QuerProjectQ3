#include "mas/domain/CapEventExtractor.hpp"
#include "mas/domain/DeltaPolicy.hpp"
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
            // One saturating policy for this extractor, the flat one and the
            // CUDA kernel; the failure it prevents is in DeltaPolicy.hpp.
            out.push_back(makeEvent(row, h, c, saturated_delta(c, *last), false));
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
