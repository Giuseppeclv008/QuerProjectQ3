#pragma once
#include <climits>

// Callable from both host TUs and CUDA kernels: the saturation policy must be
// one function, not three copies. The GPU kernel shipped the pre-saturation
// truncating cast after commit 9f0f901 fixed both CPU extractors — a wrapped
// delta fabricates small (or <= 1, aggregated-clearing) values on exactly the
// input class CapEvent.hpp documents. A single __host__ __device__ definition
// makes that divergence impossible and lets the policy be unit-tested on a
// machine with no CUDA toolchain.
#if defined(__CUDACC__)
#define MAS_HOST_DEVICE __host__ __device__
#else
#define MAS_HOST_DEVICE
#endif

namespace mas {

// Positive jump saturated to INT_MAX; held or backward counts yield 0 (the
// callers emit nothing on held and a reset event on backward). Counts are
// within ±2^53 (loader guarantee) so the subtraction is exact in long long.
MAS_HOST_DEVICE inline int saturated_delta(long long c_cur, long long c_prv) {
    const long long jump = c_cur - c_prv;
    return (jump > 0) ? static_cast<int>(jump > INT_MAX ? INT_MAX : jump) : 0;
}

}  // namespace mas

#undef MAS_HOST_DEVICE
