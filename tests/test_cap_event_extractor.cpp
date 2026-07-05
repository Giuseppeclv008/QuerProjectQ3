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

TEST(CapEventExtractor, CapturesTorqueStatusAndFaultFlag) {
    mas::CapEventExtractor ex;
    std::vector<mas::CapEvent> out;
    ex.process(makeRow("t0", {{2, 200}}, 2.0, 2.0), out);        // seed, OK
    ex.process(makeRow("t1", {{2, 201}}, 1.907, 65.0), out);     // fault cap
    ASSERT_EQ(out.size(), 1u);
    EXPECT_DOUBLE_EQ(out[0].app_torque, 1.907);
    EXPECT_DOUBLE_EQ(out[0].status, 65.0);
    EXPECT_TRUE(out[0].is_fault);
}

TEST(CapEventExtractor, CounterResetEmitsResetMarker) {
    mas::CapEventExtractor ex;
    std::vector<mas::CapEvent> out;
    ex.process(makeRow("t0", {{1, 100}}), out);
    ex.process(makeRow("t1", {{1, 40}}), out);    // reset / rollover
    ASSERT_EQ(out.size(), 1u);
    EXPECT_TRUE(out[0].reset);
    EXPECT_EQ(out[0].cap_seq, 40);
    EXPECT_EQ(out[0].delta, 0);
}

TEST(CapEventExtractor, DeltaSumEqualsFinalMinusInitialAcrossSpan) {
    mas::CapEventExtractor ex;
    std::vector<mas::CapEvent> out;
    const long long seq[] = {1000, 1000, 1001, 1001, 1004, 1004, 1005};
    for (int i = 0; i < 7; ++i)
        ex.process(makeRow("t" + std::to_string(i), {{3, seq[i]}}), out);
    long long sum = 0;
    for (const auto& e : out) sum += e.delta;
    EXPECT_EQ(sum, 1005 - 1000);   // 5 caps, independent of poll cadence
}

} // namespace
