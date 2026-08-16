// Differential test: cuda_clean_file against the production CPU pair
// (CsvRawReader + CapEventExtractor) over synthetic adversarial CSVs.
//
// The GPU pipeline had zero test coverage: its only gate was --verify inside
// mas_cuda_clean, which is not a test, runs on repeat 1 only, compares against
// the flat extractor rather than the production pair, and has only ever seen
// clean pool data. Every divergence the full-tree audit found in the CUDA
// parser (exponent forms parsed as their mantissa prefix, nan/inf/empty cells
// as 0, a missing final newline dropping a row's events, long timestamps
// silently truncated) would have been caught by one synthetic differential --
// this one. The cases below are those findings, pinned.
//
// Compiled only when MAS_ENABLE_CUDA is ON; skips (never passes vacuously)
// when no device is present, same convention as the real-data skips.
#include "../core/cuda/CudaCleaner.hpp"
#include "mas/domain/CapEventExtractor.hpp"
#include "mas/store/CsvRawReader.hpp"

#include <cuda_runtime.h>
#include <gtest/gtest.h>

#include <cmath>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

namespace {

struct Row {
    std::string ts;
    std::vector<std::string> c, t, s;   // Count / AppTorque / Status per head
    explicit Row(std::string ts_, long long count_all)
        : ts(std::move(ts_)),
          c(mas::NUM_HEADS, std::to_string(count_all)),
          t(mas::NUM_HEADS, "2.0"),
          s(mas::NUM_HEADS, "0") {}
};

std::string line_of(const Row& r) {
    std::string out = r.ts;
    for (const auto& v : r.c) out += "," + v;
    for (const auto& v : r.t) out += "," + v;
    for (const auto& v : r.s) out += "," + v;
    return out;
}

std::string header_line() {
    std::string out;
    const auto hdr = mas::CsvRawReader::expected_header();
    for (size_t i = 0; i < hdr.size(); ++i) out += (i ? "," : "") + hdr[i];
    return out;
}

std::string temp_csv_path(const std::string& name) {
    return (std::filesystem::temp_directory_path() / ("mas_cuda_diff_" + name)).string();
}

std::string write_csv(const std::string& name, const std::vector<Row>& rows,
                      const std::string& eol = "\n", bool final_eol = true) {
    const auto path = temp_csv_path(name);
    std::ofstream out(path, std::ios::binary);
    out << header_line() << eol;
    for (size_t i = 0; i < rows.size(); ++i) {
        out << line_of(rows[i]);
        if (i + 1 < rows.size() || final_eol) out << eol;
    }
    return path;
}

// Header plus `body` byte for byte -- for fixtures whose point is the exact
// line structure (blank lines, mixed line ends), which write_csv normalizes.
std::string write_raw(const std::string& name, const std::string& body) {
    const auto path = temp_csv_path(name);
    std::ofstream out(path, std::ios::binary);
    out << header_line() << "\n" << body;
    return path;
}

std::vector<mas::CapEvent> cpu_reference(const std::string& path) {
    std::vector<mas::CapEvent> out;
    mas::CsvRawReader reader(path);
    EXPECT_TRUE(reader.is_open()) << reader.header_error();
    mas::RawRow row;
    mas::CapEventExtractor ex;
    while (reader.next(row)) ex.process(row, out);
    return out;
}

// Field-by-field, bitwise on the doubles (the device keeps them as double for
// exactly this reason). NaN compares as "both NaN", never NaN == NaN.
void expect_identical(const std::vector<mas::CapEvent>& cpu,
                      const std::vector<mas::CapEvent>& gpu) {
    ASSERT_EQ(cpu.size(), gpu.size());
    for (size_t i = 0; i < cpu.size(); ++i) {
        SCOPED_TRACE("event " + std::to_string(i));
        EXPECT_EQ(cpu[i].head_id, gpu[i].head_id);
        EXPECT_EQ(cpu[i].ts, gpu[i].ts);
        EXPECT_EQ(cpu[i].cap_seq, gpu[i].cap_seq);
        EXPECT_EQ(cpu[i].delta, gpu[i].delta);
        EXPECT_EQ(cpu[i].is_fault, gpu[i].is_fault);
        EXPECT_EQ(cpu[i].aggregated, gpu[i].aggregated);
        EXPECT_EQ(cpu[i].reset, gpu[i].reset);
        if (std::isnan(cpu[i].app_torque) || std::isnan(gpu[i].app_torque))
            EXPECT_TRUE(std::isnan(cpu[i].app_torque) && std::isnan(gpu[i].app_torque));
        else
            EXPECT_EQ(cpu[i].app_torque, gpu[i].app_torque);
        if (std::isnan(cpu[i].status) || std::isnan(gpu[i].status))
            EXPECT_TRUE(std::isnan(cpu[i].status) && std::isnan(gpu[i].status));
        else
            EXPECT_EQ(cpu[i].status, gpu[i].status);
    }
}

class CudaDifferential : public ::testing::Test {
protected:
    void SetUp() override {
        int n = 0;
        if (cudaGetDeviceCount(&n) != cudaSuccess || n == 0)
            GTEST_SKIP() << "no CUDA device on this machine";
    }
    std::vector<std::string> made;
    void TearDown() override {
        for (const auto& p : made) std::remove(p.c_str());
    }
    std::string csv(const std::string& name, const std::vector<Row>& rows,
                    const std::string& eol = "\n", bool final_eol = true) {
        made.push_back(write_csv(name, rows, eol, final_eol));
        return made.back();
    }
    std::string raw(const std::string& name, const std::string& body) {
        made.push_back(write_raw(name, body));
        return made.back();
    }
};

TEST_F(CudaDifferential, CleanFileMatchesTheProductionCpuPair) {
    std::vector<Row> rows{Row("2026-02-01T00:00:00.000", 100),
                          Row("2026-02-01T00:00:01.000", 100),
                          Row("2026-02-01T00:00:02.000", 100)};
    rows[1].c[0] = "101";                       // head 1: one cap
    rows[1].t[0] = "2.0020000000000002";        // 17-digit repr: repair path
    rows[2].c[5] = "103";                       // head 6: aggregated (delta 3)
    rows[2].c[7] = "50";                        // head 8: counter reset
    const auto p = csv("clean.csv", rows);

    std::vector<mas::CapEvent> gpu;
    mas::CudaStageTimes t{};
    std::string err;
    ASSERT_TRUE(mas::cuda_clean_file(p, gpu, t, err)) << err;
    expect_identical(cpu_reference(p), gpu);
}

TEST_F(CudaDifferential, CrlfFileMatches) {
    std::vector<Row> rows{Row("2026-02-01T00:00:00.000", 10),
                          Row("2026-02-01T00:00:01.000", 10)};
    rows[1].c[2] = "11";
    const auto p = csv("crlf.csv", rows, "\r\n");

    std::vector<mas::CapEvent> gpu;
    mas::CudaStageTimes t{};
    std::string err;
    ASSERT_TRUE(mas::cuda_clean_file(p, gpu, t, err)) << err;
    expect_identical(cpu_reference(p), gpu);
}

TEST_F(CudaDifferential, HostileTorqueAndStatusCellsMatchViaTheRepairPath) {
    // Every cell here defeats the device grammar and lands in the host strtod
    // repair, and every one is legal under the shared row policy (RowParse:
    // stod parses it, the value is finite and within float range), so both
    // pipelines must emit the same values -- the audit found the GPU
    // inventing 2.5 for "2.5E-3" instead. ("nan" and "inf" used to sit in this
    // fixture; since d81f9fc RowParse fails the whole row for them, and they
    // belong to the refusal case below.)
    std::vector<Row> rows{Row("2026-02-01T00:00:00.000", 100),
                          Row("2026-02-01T00:00:01.000", 100)};
    rows[1].c[0] = "101";
    rows[1].t[0] = "2.5E-3";
    rows[1].c[3] = "101";
    rows[1].t[3] = " 1.5";     // leading whitespace
    rows[1].c[4] = "101";
    rows[1].t[4] = "1.2.3";    // stod and strtod both stop at the second dot
    rows[1].c[5] = "101";
    rows[1].s[5] = "6.5e1";    // status 65 in exponent form: is_fault flips
    const auto p = csv("hostile.csv", rows);

    std::vector<mas::CapEvent> gpu;
    mas::CudaStageTimes t{};
    std::string err;
    ASSERT_TRUE(mas::cuda_clean_file(p, gpu, t, err)) << err;
    const auto cpu = cpu_reference(p);
    ASSERT_EQ(cpu.size(), 4u);   // the row is valid: all four heads emit
    expect_identical(cpu, gpu);
}

TEST_F(CudaDifferential, ACellTheRowPolicyRejectsRefusesTheFileEvenOnASilentHead) {
    // RowParse fails the WHOLE row when any torque/status cell is not a
    // number, overflows, or is non-finite / beyond float range (BadNumeric /
    // BadReal): the CPU readers skip it and the extractor never sees it. Past
    // the delta stage the GPU cannot skip a row, so it must refuse the file --
    // and it must look at every cell of the row, not only the emitting heads':
    // here head 1 is the one that emits, and head 6 (count held, no event)
    // carries the cell that makes the CPU drop the row. Before this check the
    // GPU shipped head 1's event, and the "matches via the repair path"
    // fixture above passed nan/inf through as values.
    const std::vector<std::string> bad_cells{"nan", "inf", "1e300", "1e400", ""};
    for (size_t k = 0; k < bad_cells.size(); ++k) {
        const std::string& bad = bad_cells[k];
        SCOPED_TRACE("torque cell = \"" + bad + "\"");
        std::vector<Row> rows{Row("2026-02-01T00:00:00.000", 100),
                              Row("2026-02-01T00:00:01.000", 100)};
        rows[1].c[0] = "101";
        rows[1].t[5] = bad;
        const auto p = csv("badreal_" + std::to_string(k) + ".csv", rows);

        // The CPU side of the contract, pinned: the whole row is gone.
        EXPECT_TRUE(cpu_reference(p).empty());

        std::vector<mas::CapEvent> gpu;
        mas::CudaStageTimes t{};
        std::string err;
        EXPECT_FALSE(mas::cuda_clean_file(p, gpu, t, err));
        EXPECT_NE(err.find("row policy"), std::string::npos) << err;
    }
}

TEST_F(CudaDifferential, TrailingBlankLineIsSkippedLikeTheCpuReaders) {
    // A file ending "\n\n". Both CPU readers `continue` past a blank line; the
    // GPU indexed it as a zero-column row, flagged it fatal, and refused the
    // whole file (review I7) -- a CSV that cleaned on the CPU and not on the
    // GPU.
    std::vector<Row> rows{Row("2026-02-01T00:00:00.000", 100),
                          Row("2026-02-01T00:00:01.000", 100)};
    rows[1].c[0] = "101";
    const auto p = raw("blank_tail.csv",
                       line_of(rows[0]) + "\n" + line_of(rows[1]) + "\n\n");

    std::vector<mas::CapEvent> gpu;
    mas::CudaStageTimes t{};
    std::string err;
    ASSERT_TRUE(mas::cuda_clean_file(p, gpu, t, err)) << err;
    const auto cpu = cpu_reference(p);
    ASSERT_EQ(cpu.size(), 1u);
    expect_identical(cpu, gpu);
}

TEST_F(CudaDifferential, BlankLinesInsideTheFileAreAbsentFromTheDeltaChain) {
    // Blank lines before the first row, between rows (LF and a lone CR), and
    // two in a row. Skipping means absent: the row after a blank line takes
    // its delta against the last real row before it. A blank row left in the
    // chain as zeros would fabricate 36 resets against the row before it and
    // 36 events against the row after -- the differential would see 72+
    // events where the CPU sees 4.
    std::vector<Row> rows{Row("2026-02-01T00:00:00.000", 100),
                          Row("2026-02-01T00:00:01.000", 100),
                          Row("2026-02-01T00:00:02.000", 100),
                          Row("2026-02-01T00:00:03.000", 100)};
    rows[1].c[0] = "101";                     // head 1: one cap
    rows[2].c[0] = "102";                     // head 1: another
    rows[2].c[5] = "103";                     // head 6: aggregated (delta 3)
    rows[3].c[0] = "102";                     // heads 1 and 6 held on the last row
    rows[3].c[5] = "103";
    rows[3].c[7] = "50";                      // head 8: counter reset
    const auto p = raw("blank_mid.csv",
                       "\n" + line_of(rows[0]) + "\n" + line_of(rows[1]) + "\n" +
                       "\r\n" + line_of(rows[2]) + "\n\n\n" + line_of(rows[3]) + "\n");

    std::vector<mas::CapEvent> gpu;
    mas::CudaStageTimes t{};
    std::string err;
    ASSERT_TRUE(mas::cuda_clean_file(p, gpu, t, err)) << err;
    const auto cpu = cpu_reference(p);
    ASSERT_EQ(cpu.size(), 4u);
    expect_identical(cpu, gpu);
}

TEST_F(CudaDifferential, ExponentFormInACountCellRefusesTheFile) {
    // cap_seq and event existence derive from counts, and there is no
    // after-the-fact repair for those -- the honest response is a loud
    // refusal, never the mantissa prefix the first parser returned (1e5 -> 1,
    // which fabricated a counter reset against a neighbour of 100).
    std::vector<Row> rows{Row("2026-02-01T00:00:00.000", 100),
                          Row("2026-02-01T00:00:01.000", 100)};
    rows[1].c[0] = "1e5";
    const auto p = csv("expcount.csv", rows);

    std::vector<mas::CapEvent> gpu;
    mas::CudaStageTimes t{};
    std::string err;
    EXPECT_FALSE(mas::cuda_clean_file(p, gpu, t, err));
    EXPECT_NE(err.find("Count"), std::string::npos) << err;
}

TEST_F(CudaDifferential, EmptyCountCellRefusesTheFile) {
    // The CPU reader drops such a row silently (stod throws, the row is
    // skipped); this pipeline is past the delta stage before it can know, so
    // it refuses the whole file instead. Loud beats silent; neither invents.
    std::vector<Row> rows{Row("2026-02-01T00:00:00.000", 100),
                          Row("2026-02-01T00:00:01.000", 100)};
    rows[1].c[0] = "";
    const auto p = csv("emptycount.csv", rows);

    std::vector<mas::CapEvent> gpu;
    mas::CudaStageTimes t{};
    std::string err;
    EXPECT_FALSE(mas::cuda_clean_file(p, gpu, t, err));
}

TEST_F(CudaDifferential, MissingFinalNewlineLosesNoEvents) {
    // The last row used to fall outside the newline index and vanish -- up to
    // 36 events gone with no error and no flag, against a CPU getline that
    // returns the line normally.
    std::vector<Row> rows{Row("2026-02-01T00:00:00.000", 100),
                          Row("2026-02-01T00:00:01.000", 100)};
    rows[1].c[0] = "101";
    rows[1].c[35] = "102";
    const auto p = csv("noeol.csv", rows, "\n", /*final_eol=*/false);

    std::vector<mas::CapEvent> gpu;
    mas::CudaStageTimes t{};
    std::string err;
    ASSERT_TRUE(mas::cuda_clean_file(p, gpu, t, err)) << err;
    const auto cpu = cpu_reference(p);
    ASSERT_EQ(cpu.size(), 2u);   // the fixture's whole point is the last row
    expect_identical(cpu, gpu);
}

TEST_F(CudaDifferential, OverlongTimestampSurvivesInFull) {
    // 30 chars, past the 23-char device slot: the CPU keeps the full string,
    // and the host must restore it from the raw line rather than ship the
    // silent truncation the device buffer forces.
    std::vector<Row> rows{Row("2026-02-01T00:00:00.000", 100),
                          Row("2026-02-01T00:00:01.123456+0100", 100)};
    rows[1].c[0] = "101";
    const auto p = csv("longts.csv", rows);

    std::vector<mas::CapEvent> gpu;
    mas::CudaStageTimes t{};
    std::string err;
    ASSERT_TRUE(mas::cuda_clean_file(p, gpu, t, err)) << err;
    const auto cpu = cpu_reference(p);
    ASSERT_EQ(gpu.size(), cpu.size());
    ASSERT_FALSE(gpu.empty());
    EXPECT_EQ(gpu[0].ts, "2026-02-01T00:00:01.123456+0100");
    expect_identical(cpu, gpu);
}

TEST_F(CudaDifferential, ShortRowRefusesTheFile) {
    // The CPU readers skip a short row; the GPU would read the missing fields
    // as 0.0 and fabricate a counter reset, so it refuses the file instead.
    std::vector<Row> rows{Row("2026-02-01T00:00:00.000", 100),
                          Row("2026-02-01T00:00:01.000", 100)};
    rows[1].c[0] = "101";
    const auto p = csv("short.csv", rows);
    {
        std::ofstream out(p, std::ios::binary | std::ios::app);
        out << "2026-02-01T00:00:02.000,1,2,3\n";   // 4 of 109 columns
    }

    std::vector<mas::CapEvent> gpu;
    mas::CudaStageTimes t{};
    std::string err;
    EXPECT_FALSE(mas::cuda_clean_file(p, gpu, t, err));
    EXPECT_NE(err.find("columns"), std::string::npos) << err;
}

} // namespace
