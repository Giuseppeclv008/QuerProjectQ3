#pragma once
#include "mas/domain/CapEvent.hpp"
#include <array>
#include <optional>
#include <vector>

namespace mas {

// Stateful, per-head. Feed rows in timestamp order; emitted events are
// appended to `out`. Not thread-safe: one instance per stream/head-partition.
class CapEventExtractor {
public:
    void process(const RawRow& row, std::vector<CapEvent>& out);

private:
    std::array<std::optional<long long>, NUM_HEADS> last_count_{};
};

} // namespace mas
