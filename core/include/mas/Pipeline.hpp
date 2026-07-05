#pragma once
#include <string>

namespace mas {

// Read raw telemetry CSV at in_path, write cap_events CSV at out_path.
// Returns the number of cap events written, or -1 if in_path cannot be opened.
long long clean_file(const std::string& in_path, const std::string& out_path,
                     const std::string& machine_id);

} // namespace mas
