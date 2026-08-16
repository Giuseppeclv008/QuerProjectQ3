#include "mas/util/platform_metrics.hpp"
#include <gtest/gtest.h>
#include <chrono>
#include <string>
#include <vector>

TEST(PlatformMetrics, ReportsPlausibleValues) {
    // The first version of this test could not fail: cpu_s sums unsigned
    // rusage fields (structurally >= 0), and `> 1.0` MB is satisfied by any
    // process on any unit convention -- swap the platform divisors and macOS
    // reports 1024x too large, still green. This mechanism produces every RSS
    // and CPU number in docs/bench/, so the bounds here are two-sided against
    // the known 32 MB ballast and a real CPU burn.
    mas::metrics_init();

    // Touch 32 MB so peak RSS is bounded below by something we chose.
    std::vector<double> ballast(4 * 1024 * 1024, 1.5);
    volatile double sink = 0;
    for (std::size_t i = 0; i < ballast.size(); i += 1024) sink += ballast[i];

    // Burn measurable CPU: ~1e8 dependent adds. A 1000x unit error in cpu_s
    // (seconds vs milliseconds) cannot survive a two-sided check around this.
    for (long i = 0; i < 100000000L; ++i) sink += static_cast<double>(i & 7);
    (void)sink;

    const auto m = mas::read_metrics();
    EXPECT_GE(m.wall_s, 0.0);
    EXPECT_GT(m.cpu_s, 0.01) << "a 1e8-iteration burn registered no CPU time";
    EXPECT_LT(m.cpu_s, 120.0) << "cpu_s implausibly large for this test binary";
    EXPECT_GT(m.peak_rss_mb, 32.0) << "the 32 MB ballast must show in peak RSS";
    EXPECT_LT(m.peak_rss_mb, 16384.0)
        << "a unit mix-up (bytes vs KB vs MB) reads as an absurd peak";
}

TEST(PlatformMetrics, WallClockAdvancesBetweenSamples) {
    mas::metrics_init();
    const auto m1 = mas::read_metrics();
    const auto until = std::chrono::steady_clock::now() +
                       std::chrono::milliseconds(15);
    volatile long spin = 0;
    while (std::chrono::steady_clock::now() < until) spin = spin + 1;
    (void)spin;
    const auto m2 = mas::read_metrics();
    EXPECT_GT(m2.wall_s, m1.wall_s) << "wall_s did not advance across 15 ms";
}

TEST(PlatformMetrics, LineIsParseable) {
    const mas::ProcMetrics m{1.25, 2.5, 128.0};
    EXPECT_EQ(mas::metrics_line("clean", m),
              "metrics: tag=clean wall_s=1.250 cpu_s=2.500 peak_rss_mb=128.0");
}
