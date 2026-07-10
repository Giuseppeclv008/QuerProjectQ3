#pragma once
#include "mas/store/EventStore.hpp"
#include <string>

namespace mas {

// Read raw telemetry CSV at in_path, write every extracted cap event to
// `store` in batches. Returns the number of events written, or -1 if
// in_path cannot be opened. Store exceptions propagate to the caller.
long long clean_file(const std::string& in_path, IEventStore& store);

// CSV convenience wrapper (Plan-1 behavior): returns the event count;
// -1 if in_path cannot be opened; -2 if out_path cannot be created or a
// write fails.
long long clean_file(const std::string& in_path, const std::string& out_path,
                     const std::string& machine_id);

} // namespace mas
