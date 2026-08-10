#include "mas/domain/CapEventExtractor.hpp"
#include "mas/domain/CapEventExtractorFlat.hpp"
#include "mas/store/CsvRawReader.hpp"
#include <gtest/gtest.h>
#include <fstream>
#include <string>
#include <vector>

namespace {

// The exact loop bench_cpu_main.cpp runs per file. Duplicated here on purpose:
// the test asserts the loop's behaviour, so it must not import the binary's
// internals and assert against itself.
std::vector<mas::CapEvent> cleanInMemory(const std::string& path) {
    mas::CsvRawReader reader(path);
    std::vector<mas::CapEvent> out;
    if (!reader.is_open()) return out;
    mas::CapEventExtractor ex;
    mas::RawRow row;
    while (reader.next(row)) ex.process(row, out);
    return out;
}

} // namespace

TEST(BenchCpuParity, MatchesLoadColumnsPlusFlatOnARealDayFile) {
    const std::string p =
        "telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02/"
        "telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02-01.csv";
    std::ifstream probe(p);
    if (!probe.good()) GTEST_SKIP() << "pool not extracted: " << p;
    probe.close();

    const auto streamed = cleanInMemory(p);

    mas::RawColumns cols; std::string err;
    ASSERT_TRUE(mas::load_columns(p, cols, err)) << err;
    std::vector<mas::CapEvent> flat;
    mas::extract_flat(cols.ts, cols.count.data(), cols.torque.data(),
                      cols.status.data(), cols.n_rows, flat);

    ASSERT_EQ(streamed.size(), flat.size());
    for (std::size_t i = 0; i < streamed.size(); ++i) {
        SCOPED_TRACE("event index " + std::to_string(i));
        EXPECT_EQ(streamed[i].head_id, flat[i].head_id);
        EXPECT_EQ(streamed[i].ts, flat[i].ts);
        EXPECT_EQ(streamed[i].cap_seq, flat[i].cap_seq);
        EXPECT_DOUBLE_EQ(streamed[i].app_torque, flat[i].app_torque);
        EXPECT_EQ(streamed[i].delta, flat[i].delta);
        EXPECT_EQ(streamed[i].reset, flat[i].reset);
    }
}
