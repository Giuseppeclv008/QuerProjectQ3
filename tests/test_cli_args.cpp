// The regression these cover is not a crash but a success: `clean day.csv
// out.csv --format parquet` wrote CSV, labelled every row machine_id
// "--format", and exited 0. Nothing in the C++ suite ran an app's argv parsing
// before, because the parsing lived in main(); unconsumed_flag() is a free
// function so the rule can be tested without spawning a process.
#include "mas/apps/CliArgs.hpp"

#include <gtest/gtest.h>

#include <vector>

namespace {

// argv is char** in main(), so the fixtures are too -- string literals would
// need casting away const at every call site.
std::optional<std::string> scan(std::vector<std::string> args, int argi) {
    std::vector<char*> argv;
    for (auto& a : args) argv.push_back(a.data());
    return mas::unconsumed_flag(static_cast<int>(argv.size()), argv.data(), argi);
}

} // namespace

TEST(CliArgs, PositionalsOnlyAreAccepted) {
    EXPECT_FALSE(scan({"clean", "day.csv", "out.csv"}, 1).has_value());
    EXPECT_FALSE(scan({"clean", "day.csv", "out.csv", "MCC"}, 1).has_value());
}

TEST(CliArgs, TrailingFormatFlagIsRejected) {
    // The exact invocation that used to succeed with the wrong output format.
    const auto err = scan({"clean", "day.csv", "out.csv", "--format", "parquet"}, 1);
    ASSERT_TRUE(err.has_value());
    EXPECT_NE(err->find("--format"), std::string::npos);
    EXPECT_NE(err->find("must come before"), std::string::npos);
}

TEST(CliArgs, AFlagAlreadyConsumedIsNotRescanned) {
    // argi past the flag block is what main() passes; the leading --format is
    // the parser's own, not an unconsumed one.
    EXPECT_FALSE(scan({"clean", "--format", "parquet", "day.csv", "out.csv"}, 3).has_value());
}

TEST(CliArgs, EqualsFormSaysWhereTheValueGoes) {
    const auto err = scan({"clean", "--format=parquet", "day.csv", "out.csv"}, 1);
    ASSERT_TRUE(err.has_value());
    // Not "unexpected flag": the user's mistake is the syntax, not the place.
    EXPECT_NE(err->find("--format parquet"), std::string::npos);
    EXPECT_EQ(err->find("unexpected flag"), std::string::npos);
}

TEST(CliArgs, UnknownFlagAnywhereIsRejected) {
    const auto err = scan({"mas_worker", "tcp://a", "--bogus"}, 1);
    ASSERT_TRUE(err.has_value());
    EXPECT_NE(err->find("--bogus"), std::string::npos);
}

TEST(CliArgs, ASingleDashIsAPositional) {
    // Only "--" prefixes are flags: a lone "-" is a legitimate stdin
    // convention and a negative number is a legitimate value.
    EXPECT_FALSE(scan({"clean", "-", "out.csv"}, 1).has_value());
    EXPECT_FALSE(scan({"clean", "day.csv", "-1"}, 1).has_value());
}
