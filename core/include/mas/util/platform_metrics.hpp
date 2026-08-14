#pragma once
// Self-reported process metrics. Replaces the external timing wrapper:
// bench/run_bench.sh used `/usr/bin/time -l`, which is BSD-only -- the flag
// does not exist in GNU coreutils and has no Windows equivalent at all.
// This is the only #ifdef _WIN32 in the codebase (spec §10 R3).
#include <chrono>
#include <cstdio>
#include <string>

#ifdef _WIN32
#  ifndef WIN32_LEAN_AND_MEAN
#    define WIN32_LEAN_AND_MEAN
#  endif
#  ifndef NOMINMAX
#    define NOMINMAX   // windows.h's min/max macros would break std::min in any TU including this
#  endif
#  include <windows.h>
#  include <psapi.h>
#else
#  include <sys/resource.h>
#  include <sys/time.h>
#endif

namespace mas {

struct ProcMetrics {
    double wall_s = 0.0;
    double cpu_s = 0.0;        // user + system
    double peak_rss_mb = 0.0;
};

namespace detail {
inline std::chrono::steady_clock::time_point& start_time() {
    static std::chrono::steady_clock::time_point t0 = std::chrono::steady_clock::now();
    return t0;
}
} // namespace detail

// Call once at the top of main() to anchor wall_s. Safe to omit -- the anchor
// is then the first read_metrics() call.
inline void metrics_init() { (void)detail::start_time(); }

inline ProcMetrics read_metrics() {
    ProcMetrics m;
    // Read the anchor into a local first. Operand evaluation order in `a - b` is
    // unspecified, so inlining start_time() there lets the lazy initialiser stamp
    // the anchor *after* the sample when metrics_init() was never called --
    // which yields a negative wall_s.
    const auto t0 = detail::start_time();
    m.wall_s = std::chrono::duration<double>(
        std::chrono::steady_clock::now() - t0).count();
#ifdef _WIN32
    FILETIME creation, exit, kernel, user;
    if (GetProcessTimes(GetCurrentProcess(), &creation, &exit, &kernel, &user)) {
        auto to_s = [](const FILETIME& ft) {
            ULARGE_INTEGER v;
            v.LowPart = ft.dwLowDateTime;
            v.HighPart = ft.dwHighDateTime;
            return static_cast<double>(v.QuadPart) * 1e-7;   // 100 ns ticks
        };
        m.cpu_s = to_s(kernel) + to_s(user);
    }
    PROCESS_MEMORY_COUNTERS pmc{};
    if (GetProcessMemoryInfo(GetCurrentProcess(), &pmc, sizeof(pmc)))
        m.peak_rss_mb = static_cast<double>(pmc.PeakWorkingSetSize) / 1048576.0;
#else
    struct rusage ru {};
    if (getrusage(RUSAGE_SELF, &ru) == 0) {
        m.cpu_s = static_cast<double>(ru.ru_utime.tv_sec + ru.ru_stime.tv_sec) +
                  1e-6 * static_cast<double>(ru.ru_utime.tv_usec + ru.ru_stime.tv_usec);
        // ru_maxrss is bytes on macOS, kilobytes on Linux.
#  ifdef __APPLE__
        m.peak_rss_mb = static_cast<double>(ru.ru_maxrss) / 1048576.0;
#  else
        m.peak_rss_mb = static_cast<double>(ru.ru_maxrss) / 1024.0;
#  endif
    }
#endif
    return m;
}

// One machine-readable line on stderr, parsed by bench/run_bench_cuda.py.
inline std::string metrics_line(const std::string& tag, const ProcMetrics& m) {
    char buf[256];
    std::snprintf(buf, sizeof(buf),
                  "metrics: tag=%s wall_s=%.3f cpu_s=%.3f peak_rss_mb=%.1f",
                  tag.c_str(), m.wall_s, m.cpu_s, m.peak_rss_mb);
    return std::string(buf);
}

} // namespace mas
