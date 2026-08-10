#pragma once
#include "mas/domain/CapEvent.hpp"
#include <string>
#include <vector>

namespace mas {

// Per-stage GPU timings, seconds. Spec §6.4: this breakdown is the point --
// it says how much of the win is "GPU parses the CSV" versus "GPU does the
// compare", which is what justifies porting the transform at all.
struct CudaStageTimes {
    double read_s = 0, h2d_s = 0, index_s = 0, parse_s = 0,
           delta_s = 0, compact_s = 0, d2h_s = 0;
};

// Clean one raw telemetry day-file on the GPU. Appends events in
// (row asc, head asc) order -- identical to CapEventExtractor's. Returns false
// and fills `error` on any failure.
bool cuda_clean_file(const std::string& path, std::vector<CapEvent>& out,
                     CudaStageTimes& times, std::string& error);

} // namespace mas
