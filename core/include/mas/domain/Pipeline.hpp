#pragma once
#include "mas/store/EventStore.hpp"
#include <string>

namespace mas {

// Per-file input-quality counters, surfaced so a truncated or disordered
// parse is distinguishable from a clean one. The counters existed (the README
// advertises "skips malformed rows with counter") but no production call site
// read them.
struct CleanFileStats {
    std::size_t skipped_rows = 0;       // dropped by the shared validity policy
    std::size_t out_of_order_rows = 0;  // accepted, but ts <= previous row's
};

// Read raw telemetry CSV at in_path, write every extracted cap event to
// `store` in batches. Returns the number of events written, or -1 if
// in_path cannot be opened. Store exceptions propagate to the caller.
long long clean_file(const std::string& in_path, IEventStore& store,
                     CleanFileStats* stats = nullptr);

// CSV convenience wrapper: returns the event count;
// -1 if in_path cannot be opened; -2 if out_path cannot be created or a
// write fails.
long long clean_file(const std::string& in_path, const std::string& out_path,
                     const std::string& machine_id);

} // namespace mas
