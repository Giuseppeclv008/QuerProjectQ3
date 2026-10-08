#include "mas/store/RawInput.hpp"
#include "mas/domain/Pipeline.hpp"
#include "mas/store/CsvRawReader.hpp"
#include "mas/store/EventStore.hpp"
#include "mas/store/SqlQuote.hpp"
#include "fakes/TempPath.hpp"
#include <duckdb.hpp>
#include <gtest/gtest.h>
#include <fstream>
#include <string>
#include <vector>

namespace {

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

// Head 1 carries the signal, every other head is zero.
std::string rawLine(const std::string& ts, const std::string& count,
                    const std::string& torque = "1.5",
                    const std::string& status = "0.0") {
    std::string s = ts;
    for (int i = 0; i < 36; ++i) s += "," + (i == 0 ? count : std::string("0.0"));
    for (int i = 0; i < 36; ++i) s += "," + (i == 0 ? torque : std::string("0.0"));
    for (int i = 0; i < 36; ++i) s += "," + (i == 0 ? status : std::string("0.0"));
    return s;
}

// Four polls: a cap, an aggregated jump of 2, a fault, in the pool's own
// timestamp spelling.
std::string poolCsv() {
    return realHeader() + "\n" +
           rawLine("2026-02-27T16:00:00.000", "100.0") + "\n" +
           rawLine("2026-02-27T16:00:01.000", "101.0", "1.75") + "\n" +
           rawLine("2026-02-27T16:00:02.000", "103.0", "2.5") + "\n" +
           rawLine("2026-02-27T16:00:03.000", "104.0", "0.5", "1.0") + "\n";
}

void duck(const std::string& sql) {
    duckdb::DuckDB db(nullptr);
    duckdb::Connection con(db);
    auto r = con.Query(sql);
    ASSERT_FALSE(r->HasError()) << r->GetError();
}

std::string lit(const std::string& p) { return "'" + mas::sql_quote(p) + "'"; }

std::vector<mas::RawRow> readAll(mas::RawReader& r) {
    std::vector<mas::RawRow> rows;
    mas::RawRow row;
    while (r.next(row)) rows.push_back(row);
    return rows;
}

void expectSameRows(const std::vector<mas::RawRow>& a,
                    const std::vector<mas::RawRow>& b) {
    ASSERT_EQ(a.size(), b.size());
    for (std::size_t i = 0; i < a.size(); ++i) {
        EXPECT_EQ(a[i].ts, b[i].ts) << "row " << i;
        EXPECT_EQ(a[i].count, b[i].count) << "row " << i;
        EXPECT_EQ(a[i].torque, b[i].torque) << "row " << i;
        EXPECT_EQ(a[i].status, b[i].status) << "row " << i;
    }
}

struct CapturingStore : mas::IEventStore {
    std::vector<mas::CapEvent> events;
    void write(std::span<const mas::CapEvent> e) override {
        events.insert(events.end(), e.begin(), e.end());
    }
};

// The CSV day-file and its Parquet and JSON twins, built by DuckDB the way
// someone handing the pool over in another format would.
struct Twins {
    std::string csv, parquet, json;
    Twins(const std::string& tag) {
        csv = mas::test::temp_artifact(tag + ".csv");
        parquet = mas::test::temp_artifact(tag + ".parquet");
        json = mas::test::temp_artifact(tag + ".json");
        writeFile(csv, poolCsv());
        // read_csv types `timestamp` as TIMESTAMP: the Parquet column is typed
        // and the JSON writer spells it "2026-02-27 16:00:00".
        duck("COPY (SELECT * FROM read_csv(" + lit(csv) + ")) TO " + lit(parquet) +
             " (FORMAT PARQUET)");
        duck("COPY (SELECT * FROM read_csv(" + lit(csv) + ")) TO " + lit(json) +
             " (FORMAT JSON)");
    }
    ~Twins() {
        std::remove(csv.c_str());
        std::remove(parquet.c_str());
        std::remove(json.c_str());
    }
};

TEST(RawInput, FormatFollowsExtensionCaseInsensitive) {
    EXPECT_EQ(mas::raw_format_for("d/2026-02-01.csv"), mas::RawFormat::Csv);
    EXPECT_EQ(mas::raw_format_for("d/2026-02-01.parquet"), mas::RawFormat::Parquet);
    EXPECT_EQ(mas::raw_format_for("d/2026-02-01.PARQUET"), mas::RawFormat::Parquet);
    EXPECT_EQ(mas::raw_format_for("d/2026-02-01.json"), mas::RawFormat::Json);
    EXPECT_EQ(mas::raw_format_for("d/2026-02-01.jsonl"), mas::RawFormat::Json);
    EXPECT_EQ(mas::raw_format_for("d/2026-02-01.ndjson"), mas::RawFormat::Json);
    EXPECT_EQ(mas::raw_format_for("d/no_extension"), mas::RawFormat::Csv);
}

TEST(RawInput, ParquetAndJsonYieldTheCsvRows) {
    Twins t("raw_input_twins");
    mas::CsvRawReader csv(t.csv);
    ASSERT_TRUE(csv.is_open());
    const auto want = readAll(csv);
    ASSERT_EQ(want.size(), 4u);

    for (const auto& path : {t.parquet, t.json}) {
        SCOPED_TRACE(path);
        auto r = mas::open_raw_reader(path);
        ASSERT_TRUE(r->is_open()) << r->header_error();
        expectSameRows(readAll(*r), want);
        EXPECT_EQ(r->skipped(), 0u);
        EXPECT_EQ(r->out_of_order(), 0u);
    }
}

TEST(RawInput, CleanFileGivesIdenticalEventsInEveryFormat) {
    Twins t("raw_input_clean");
    CapturingStore from_csv;
    ASSERT_GT(mas::clean_file(t.csv, from_csv), 0);
    for (const auto& path : {t.parquet, t.json}) {
        SCOPED_TRACE(path);
        CapturingStore s;
        EXPECT_EQ(mas::clean_file(path, s),
                  static_cast<long long>(from_csv.events.size()));
        ASSERT_EQ(s.events.size(), from_csv.events.size());
        for (std::size_t i = 0; i < s.events.size(); ++i) {
            const auto& a = s.events[i];
            const auto& b = from_csv.events[i];
            EXPECT_EQ(a.head_id, b.head_id);
            EXPECT_EQ(a.ts, b.ts);
            EXPECT_EQ(a.cap_seq, b.cap_seq);
            EXPECT_EQ(a.app_torque, b.app_torque);
            EXPECT_EQ(a.status, b.status);
            EXPECT_EQ(a.delta, b.delta);
            EXPECT_EQ(a.is_fault, b.is_fault);
            EXPECT_EQ(a.aggregated, b.aggregated);
            EXPECT_EQ(a.reset, b.reset);
        }
    }
}

TEST(RawInput, ColumnsAreMatchedByNameNotPosition) {
    Twins t("raw_input_order");
    const std::string shuffled = mas::test::temp_artifact("raw_input_shuffled.parquet");
    // Status first, timestamp last: positional reading would take statuses
    // for counts.
    std::string cols;
    const auto h = mas::CsvRawReader::expected_header();
    for (std::size_t i = h.size(); i-- > 0;) cols += (cols.empty() ? "" : ", ") + ("\"" + h[i] + "\"");
    duck("COPY (SELECT " + cols + " FROM read_parquet(" + lit(t.parquet) + ")) TO " +
         lit(shuffled) + " (FORMAT PARQUET)");

    mas::CsvRawReader csv(t.csv);
    auto r = mas::open_raw_reader(shuffled);
    ASSERT_TRUE(r->is_open()) << r->header_error();
    expectSameRows(readAll(*r), readAll(csv));
    std::remove(shuffled.c_str());
}

TEST(RawInput, MissingColumnIsRefusedAndNamed) {
    Twins t("raw_input_missing");
    const std::string p = mas::test::temp_artifact("raw_input_missing_col.parquet");
    duck("COPY (SELECT * EXCLUDE (\"H07 AppTorque\") FROM read_parquet(" + lit(t.parquet) +
         ")) TO " + lit(p) + " (FORMAT PARQUET)");
    auto r = mas::open_raw_reader(p);
    EXPECT_FALSE(r->is_open());
    EXPECT_NE(r->header_error().find("missing column 'H07 AppTorque'"), std::string::npos)
        << r->header_error();
    mas::RawRow row;
    EXPECT_FALSE(r->next(row));
    std::remove(p.c_str());
}

TEST(RawInput, ExtraColumnIsRefusedAndNamed) {
    Twins t("raw_input_extra");
    const std::string p = mas::test::temp_artifact("raw_input_extra_col.json");
    duck("COPY (SELECT *, 7 AS operator_id FROM read_parquet(" + lit(t.parquet) +
         ")) TO " + lit(p) + " (FORMAT JSON)");
    auto r = mas::open_raw_reader(p);
    EXPECT_FALSE(r->is_open());
    EXPECT_NE(r->header_error().find("unexpected column 'operator_id'"), std::string::npos)
        << r->header_error();
    std::remove(p.c_str());
}

TEST(RawInput, MalformedJsonCellsSkipTheRowNotTheFile) {
    // A JSON array (not newline-delimited) with a string, a null and an
    // out-of-range torque among good rows: the CSV policy, row by row.
    const auto h = mas::CsvRawReader::expected_header();
    const auto obj = [&](const std::string& ts, const std::string& first_torque) {
        std::string o = "{\"" + h[0] + "\": \"" + ts + "\"";
        for (std::size_t i = 1; i < h.size(); ++i)
            o += ", \"" + h[i] + "\": " + (i == 37 ? first_torque : std::string("1.0"));
        return o + "}";
    };
    const std::string p = mas::test::temp_artifact("raw_input_bad_cells.json");
    writeFile(p, "[" + obj("2026-02-27T16:00:00.000", "1.0") + ",\n" +
                     obj("2026-02-27T16:00:01.000", "\"abc\"") + ",\n" +
                     obj("2026-02-27T16:00:02.000", "null") + ",\n" +
                     obj("2026-02-27T16:00:03.000", "1e300") + ",\n" +
                     obj("2026-02-27T16:00:04.000", "2.0") + "]");
    auto r = mas::open_raw_reader(p);
    ASSERT_TRUE(r->is_open()) << r->header_error();
    const auto rows = readAll(*r);
    ASSERT_EQ(rows.size(), 2u);
    EXPECT_EQ(rows[0].ts, "2026-02-27T16:00:00.000");
    EXPECT_EQ(rows[1].ts, "2026-02-27T16:00:04.000");
    EXPECT_DOUBLE_EQ(rows[1].torque[0], 2.0);
    EXPECT_EQ(r->skipped(), 3u);
    std::remove(p.c_str());
}

TEST(RawInput, OutOfOrderTimestampsAreCounted) {
    const std::string csv = mas::test::temp_artifact("raw_input_ooo.csv");
    const std::string pq = mas::test::temp_artifact("raw_input_ooo.parquet");
    writeFile(csv, realHeader() + "\n" + rawLine("2026-02-27T16:00:05.000", "1.0") +
                       "\n" + rawLine("2026-02-27T16:00:04.000", "2.0") + "\n");
    duck("COPY (SELECT * FROM read_csv(" + lit(csv) + ")) TO " + lit(pq) +
         " (FORMAT PARQUET)");
    auto r = mas::open_raw_reader(pq);
    ASSERT_TRUE(r->is_open()) << r->header_error();
    EXPECT_EQ(readAll(*r).size(), 2u);
    EXPECT_EQ(r->out_of_order(), 1u);
    std::remove(csv.c_str());
    std::remove(pq.c_str());
}

TEST(RawInput, MissingFileIsNotOpenWithoutHeaderError) {
    for (const char* p : {"definitely_missing.parquet", "definitely_missing.json"}) {
        auto r = mas::open_raw_reader(p);
        EXPECT_FALSE(r->is_open()) << p;
        EXPECT_TRUE(r->header_error().empty()) << p;
        CapturingStore s;
        EXPECT_EQ(mas::clean_file(p, s), -1) << p;
    }
}

TEST(RawInput, CorruptParquetIsNotOpenAndSaysWhy) {
    const std::string p = mas::test::temp_artifact("raw_input_corrupt.parquet");
    writeFile(p, "this is not a parquet file");
    auto r = mas::open_raw_reader(p);
    EXPECT_FALSE(r->is_open());
    EXPECT_NE(r->header_error().find(p), std::string::npos) << r->header_error();
    std::remove(p.c_str());
}

} // namespace
