#include "mas/util/platform_metrics.hpp"
#include <gtest/gtest.h>
#include <string>
#include <vector>

TEST(PlatformMetrics, ReportsPlausibleValues) {
    // Touch some memory so peak RSS is certainly non-trivial.
    std::vector<double> ballast(4 * 1024 * 1024, 1.5);
    volatile double sink = 0;
    for (std::size_t i = 0; i < ballast.size(); i += 1024) sink += ballast[i];
    (void)sink;

    const auto m = mas::read_metrics();
    EXPECT_GE(m.wall_s, 0.0);
    EXPECT_GE(m.cpu_s, 0.0);
    EXPECT_GT(m.peak_rss_mb, 1.0) << "32 MB ballast should show in peak RSS";
}

TEST(PlatformMetrics, LineIsParseable) {
    const mas::ProcMetrics m{1.25, 2.5, 128.0};
    EXPECT_EQ(mas::metrics_line("clean", m),
              "metrics: tag=clean wall_s=1.250 cpu_s=2.500 peak_rss_mb=128.0");
}
