#include "mas/CapEventExtractor.hpp"
#include <gtest/gtest.h>
#include <initializer_list>
#include <string>
#include <utility>
#include <vector>

namespace {

// Build a RawRow. `caps` sets Count for the given 1-based heads; torque/status
// apply to those heads too. Unlisted heads stay at 0.
mas::RawRow makeRow(const std::string& ts,
                    std::initializer_list<std::pair<int, long long>> caps,
                    double torque = 2.0, double status = 2.0) {
    mas::RawRow r;
    r.ts = ts;
    for (const auto& [head, count] : caps) {
        r.count[head - 1] = static_cast<double>(count);
        r.torque[head - 1] = torque;
        r.status[head - 1] = status;
    }
    return r;
}

TEST(CapEventExtractor, EmitsOneEventOnSingleIncrement) {
    mas::CapEventExtractor ex;
    std::vector<mas::CapEvent> out;
    ex.process(makeRow("t0", {{1, 100}}), out);   // seed
    ex.process(makeRow("t1", {{1, 101}}), out);   // increment
    ASSERT_EQ(out.size(), 1u);
    EXPECT_EQ(out[0].head_id, 1);
    EXPECT_EQ(out[0].cap_seq, 101);
    EXPECT_FALSE(out[0].reset);
}

TEST(CapEventExtractor, HeldRowsEmitNothing) {
    mas::CapEventExtractor ex;
    std::vector<mas::CapEvent> out;
    ex.process(makeRow("t0", {{1, 100}}), out);
    ex.process(makeRow("t1", {{1, 100}}), out);   // held
    ex.process(makeRow("t2", {{1, 100}}), out);   // held
    EXPECT_TRUE(out.empty());
}

TEST(CapEventExtractor, SeedRowEmitsNothing) {
    mas::CapEventExtractor ex;
    std::vector<mas::CapEvent> out;
    ex.process(makeRow("t0", {{1, 500}}), out);   // first observation
    EXPECT_TRUE(out.empty());
}

TEST(CapEventExtractor, HeadsAreIndependent) {
    mas::CapEventExtractor ex;
    std::vector<mas::CapEvent> out;
    ex.process(makeRow("t0", {{1, 10}, {5, 70}}), out);   // seed both
    ex.process(makeRow("t1", {{1, 11}, {5, 70}}), out);   // head 1 +1, head 5 held
    ASSERT_EQ(out.size(), 1u);
    EXPECT_EQ(out[0].head_id, 1);
}

TEST(CapEventExtractor, SingleIncrementHasDeltaOne) {
    mas::CapEventExtractor ex;
    std::vector<mas::CapEvent> out;
    ex.process(makeRow("t0", {{1, 100}}), out);
    ex.process(makeRow("t1", {{1, 101}}), out);
    ASSERT_EQ(out.size(), 1u);
    EXPECT_EQ(out[0].delta, 1);
    EXPECT_FALSE(out[0].aggregated);
}

TEST(CapEventExtractor, DeltaGreaterThanOneIsAggregated) {
    mas::CapEventExtractor ex;
    std::vector<mas::CapEvent> out;
    ex.process(makeRow("t0", {{1, 100}}), out);
    ex.process(makeRow("t1", {{1, 103}}), out);   // production faster than polling
    ASSERT_EQ(out.size(), 1u);
    EXPECT_EQ(out[0].delta, 3);
    EXPECT_TRUE(out[0].aggregated);
}

} // namespace
