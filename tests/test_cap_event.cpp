#include <gtest/gtest.h>
#include "mas/domain/CapEvent.hpp"

// Semantics measured at closure over 2026-02-01 (765,711 closures), spec §3.1:
//   status 0 + torque > 0  -> real capping operation (427,643/day, mean 1.998 Nm)
//   status 2 + torque == 0 -> No-Load cycle          (337,772/day)
//   status 65              -> fault                  (4/day)
// The pre-existing header comment had 0 and 2 exactly backwards.

TEST(CapEventSemantics, StatusZeroWithTorqueIsSuccessfulCap) {
    EXPECT_TRUE(mas::is_successful_cap(0.0, 1.998));
    EXPECT_FALSE(mas::is_no_load(0.0, 1.998));
}

TEST(CapEventSemantics, StatusTwoWithZeroTorqueIsNoLoad) {
    EXPECT_TRUE(mas::is_no_load(2.0, 0.0));
    EXPECT_FALSE(mas::is_successful_cap(2.0, 0.0));
}

TEST(CapEventSemantics, FaultIsNeitherSuccessfulNorNoLoad) {
    EXPECT_TRUE(mas::is_reject(65.0));
    EXPECT_FALSE(mas::is_successful_cap(65.0, 1.997));
    EXPECT_FALSE(mas::is_no_load(65.0, 1.997));
}

TEST(CapEventSemantics, TransitionArtifactsAreNeither) {
    // 137 closures/day carry status 0 with zero torque; 155 carry status 2 with
    // torque. Both are transition artifacts and must not be counted either way.
    EXPECT_FALSE(mas::is_successful_cap(0.0, 0.0));
    EXPECT_FALSE(mas::is_no_load(0.0, 0.0));
    EXPECT_FALSE(mas::is_successful_cap(2.0, 1.998));
    EXPECT_FALSE(mas::is_no_load(2.0, 1.998));
}

TEST(CapEvent, RejectBitIsBitZeroNotStatus65) {
    // Every "Reject Signal = YES" row of the brief's table has an odd status.
    for (double s : {3.0, 5.0, 9.0, 17.0, 33.0, 65.0}) {
        EXPECT_TRUE(mas::is_reject(s)) << "status " << s;
    }
    // Every "NO" row is even.
    for (double s : {0.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0}) {
        EXPECT_FALSE(mas::is_reject(s)) << "status " << s;
    }
}
