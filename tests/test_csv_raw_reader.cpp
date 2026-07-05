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

std::string fullRow(const std::string& ts, const std::string& badField = "") {
    // 109 fields; if badField nonempty it lands in the first torque slot
    std::string row = ts;
    for (int i = 0; i < 36; ++i) row += ",1.0";
    for (int i = 0; i < 36; ++i) row += (i == 0 && !badField.empty()) ? ("," + badField) : ",2.0";
    for (int i = 0; i < 36; ++i) row += ",0.0";
    return row;
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

TEST(CsvRawReader, SkipsMalformedAndShortRows) {
    std::string header = "timestamp";
    for (int i = 0; i < 108; ++i) header += ",c";
    const std::string body = header + "\n"
        + "t-short,1.0,2.0\n"                    // too few fields
        + fullRow("t-bad", "abc") + "\n"         // malformed numeric cell
        + fullRow("t-good") + "\n";
    const std::string path = "test_reader_malformed.csv";
    writeFile(path, body);

    mas::CsvRawReader reader(path);
    mas::RawRow r;
    ASSERT_TRUE(reader.next(r));                 // lands on the good row
    EXPECT_EQ(r.ts, "t-good");
    EXPECT_FALSE(reader.next(r));
    EXPECT_EQ(reader.skipped(), 2u);
    std::remove(path.c_str());
}

TEST(CsvRawReader, MissingFileNotOpenAndNextFalse) {
    mas::CsvRawReader reader("definitely_missing_file.csv");
    EXPECT_FALSE(reader.is_open());
    mas::RawRow r;
    EXPECT_FALSE(reader.next(r));
    EXPECT_EQ(reader.skipped(), 0u);
}

} // namespace
