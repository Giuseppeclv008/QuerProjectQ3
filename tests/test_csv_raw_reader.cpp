#include "mas/CsvRawReader.hpp"
#include <gtest/gtest.h>
#include <cstdio>
#include <fstream>
#include <string>

namespace {

void writeFile(const std::string& path, const std::string& body) {
    std::ofstream o(path);
    o << body;
}

TEST(CsvRawReader, ParsesTimestampAndPerHeadColumns) {
    std::string header = "timestamp";
    for (int i = 0; i < 36; ++i) header += ",H Count";
    for (int i = 0; i < 36; ++i) header += ",H AppTorque";
    for (int i = 0; i < 36; ++i) header += ",H Status";

    std::string row = "2026-02-01T10:00:00.000";
    for (int i = 0; i < 36; ++i) row += (i == 0) ? ",100.0" : ",0.0";   // counts
    for (int i = 0; i < 36; ++i) row += (i == 0) ? ",1.5"   : ",0.0";   // torque
    for (int i = 0; i < 36; ++i) row += (i == 0) ? ",65.0"  : ",0.0";   // status

    const std::string path = "test_reader_input.csv";
    writeFile(path, header + "\n" + row + "\n");

    mas::CsvRawReader reader(path);
    mas::RawRow r;
    ASSERT_TRUE(reader.next(r));
    EXPECT_EQ(r.ts, "2026-02-01T10:00:00.000");
    EXPECT_DOUBLE_EQ(r.count[0], 100.0);
    EXPECT_DOUBLE_EQ(r.torque[0], 1.5);
    EXPECT_DOUBLE_EQ(r.status[0], 65.0);
    EXPECT_FALSE(reader.next(r));   // only one data row

    std::remove(path.c_str());
}

} // namespace
