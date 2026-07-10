#pragma once
#include "mas/domain/CapEvent.hpp"
#include <span>

namespace mas {

// Persistence seam (DIP, spec §7): the pipeline writes events through this
// interface and never sees CSV/DuckDB. Implementations own machine_id.
struct IEventStore {
    virtual void write(std::span<const CapEvent> events) = 0;
    virtual ~IEventStore() = default;
};

} // namespace mas
