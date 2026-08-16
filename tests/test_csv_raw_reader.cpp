#include "mas/store/CsvRawReader.hpp"
#include <gtest/gtest.h>
#include <cstdio>
#include <fstream>
#include <string>

namespace {

// The reader now validates the header, so fixtures must carry the real AROL
// column names instead of 108 placeholder "c" columns. Built from the reader's
// own expected_header() so the two can never drift apart.
std::string realHeader() {
    const auto cols = mas::CsvRawReader::expected_header();
    std::string h;
    for (std::size_t i = 0; i < cols.size(); ++i) {
        if (i) h += ",";
        h += cols[i];
    }
    return h;
}

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
    std::string header = realHeader();

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
    std::string header = realHeader();
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


TEST(CsvRawReader, RejectsAHeaderThatIsNotTheArolLayout) {
    // A pool delivered with a different column order used to be parsed as if it
    // were this one — the header was read and discarded — so Count columns would
    // be read as torques with nothing downstream able to notice.
    const std::string path = "t_raw_bad_header.csv";
    writeFile(path, "timestamp,nope,alsonope\n2026-02-01T00:00:00.000,1,2\n");
    mas::CsvRawReader r(path);
    EXPECT_FALSE(r.is_open());
    EXPECT_NE(r.header_error().find("3 columns, expected 109"), std::string::npos)
        << "error was: " << r.header_error();
    mas::RawRow row;
    EXPECT_FALSE(r.next(row));
    std::remove(path.c_str());
}

TEST(CsvRawReader, RejectsRightColumnCountWithWrongNames) {
    // The dangerous case: 109 columns, so any shape-only check passes, but the
    // layout is the brief's slide-4 interleaved order rather than the grouped
    // one actually delivered. Counts would be read as torques at full speed.
    const std::string path = "t_raw_swapped_header.csv";
    auto cols = mas::CsvRawReader::expected_header();
    std::swap(cols[1], cols[37]);   // "H01 Count" <-> "H01 AppTorque"
    std::string h;
    for (std::size_t i = 0; i < cols.size(); ++i) { if (i) h += ","; h += cols[i]; }
    writeFile(path, h + "\n");
    mas::CsvRawReader r(path);
    EXPECT_FALSE(r.is_open());
    EXPECT_NE(r.header_error().find("column 1"), std::string::npos)
        << "error was: " << r.header_error();
    std::remove(path.c_str());
}

TEST(CsvRawReader, AcceptsTheRealArolHeader) {
    const std::string path = "t_raw_good_header.csv";
    writeFile(path, realHeader() + "\n");
    mas::CsvRawReader r(path);
    EXPECT_TRUE(r.is_open());
    EXPECT_TRUE(r.header_error().empty()) << r.header_error();
    std::remove(path.c_str());
}

TEST(CsvRawReader, RowsWithNonCounterCountsAreSkippedLikeAnyMalformedRow) {
    // stod accepts "1e18", "inf" and "nan" for a Count cell, and llround on
    // those is unspecified -- downstream they fabricated events (counts
    // 100 -> 5e9 emitted delta=705032604). Such a row is as unusable as one
    // with a non-numeric cell: skipped, and counted as skipped.
    const auto countRow = [](const std::string& ts, const std::string& c0) {
        std::string row = ts;
        for (int i = 0; i < 36; ++i) row += (i == 0) ? ("," + c0) : ",0.0";
        for (int i = 0; i < 36; ++i) row += ",2.0";
        for (int i = 0; i < 36; ++i) row += ",0.0";
        return row;
    };
    const std::string path = "t_csv_badcount.csv";
    writeFile(path, realHeader() + "\n" +
                        countRow("2026-02-01T10:00:00.000", "100") + "\n" +
                        countRow("2026-02-01T10:00:01.000", "1e18") + "\n" +
                        countRow("2026-02-01T10:00:02.000", "inf") + "\n" +
                        countRow("2026-02-01T10:00:03.000", "nan") + "\n" +
                        countRow("2026-02-01T10:00:04.000", "101") + "\n");
    mas::CsvRawReader reader(path);
    ASSERT_TRUE(reader.is_open());
    mas::RawRow r;
    int rows = 0;
    while (reader.next(r)) ++rows;
    EXPECT_EQ(rows, 2);
    EXPECT_EQ(reader.skipped(), 3u);
    std::remove(path.c_str());
}

} // namespace