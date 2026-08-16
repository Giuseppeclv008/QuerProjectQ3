#include "mas/domain/CapEventExtractor.hpp"
#include "mas/domain/CapEventExtractorFlat.hpp"
#include "mas/domain/DeltaPolicy.hpp"
#include "fakes/TempPath.hpp"
#include <gtest/gtest.h>
#include <climits>
#include <filesystem>
#include <cstdio>
#include <fstream>
#include <initializer_list>
#include <string>
#include <utility>
#include <vector>

namespace {

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

std::vector<mas::CapEvent> stateful(const std::vector<mas::RawRow>& rows) {
    mas::CapEventExtractor ex;
    std::vector<mas::CapEvent> out;
    for (const auto& r : rows) ex.process(r, out);
    return out;
}

std::vector<mas::CapEvent> flat(const std::vector<mas::RawRow>& rows) {
    std::vector<std::string> ts;
    std::vector<double> count, torque, status;
    for (const auto& r : rows) {
        ts.push_back(r.ts);
        for (int h = 0; h < mas::NUM_HEADS; ++h) {
            count.push_back(r.count[h]);
            torque.push_back(r.torque[h]);
            status.push_back(r.status[h]);
        }
    }
    std::vector<mas::CapEvent> out;
    mas::extract_flat(ts, count.data(), torque.data(), status.data(), rows.size(), out);
    return out;
}

void expectSame(const std::vector<mas::CapEvent>& a,
                const std::vector<mas::CapEvent>& b) {
    ASSERT_EQ(a.size(), b.size());
    for (std::size_t i = 0; i < a.size(); ++i) {
        SCOPED_TRACE("event index " + std::to_string(i));
        EXPECT_EQ(a[i].head_id, b[i].head_id);
        EXPECT_EQ(a[i].ts, b[i].ts);
        EXPECT_EQ(a[i].cap_seq, b[i].cap_seq);
        EXPECT_DOUBLE_EQ(a[i].app_torque, b[i].app_torque);
        EXPECT_DOUBLE_EQ(a[i].status, b[i].status);
        EXPECT_EQ(a[i].delta, b[i].delta);
        EXPECT_EQ(a[i].is_fault, b[i].is_fault);
        EXPECT_EQ(a[i].aggregated, b[i].aggregated);
        EXPECT_EQ(a[i].reset, b[i].reset);
    }
}

void expectAgrees(const std::vector<mas::RawRow>& rows) {
    expectSame(stateful(rows), flat(rows));
}

} // namespace

TEST(ExtractorFlat, EmptyInput)      { expectAgrees({}); }
TEST(ExtractorFlat, SingleSeedRow)   { expectAgrees({makeRow("t0", {{1, 100}})}); }

TEST(ExtractorFlat, SingleIncrement) {
    expectAgrees({makeRow("t0", {{1, 100}}), makeRow("t1", {{1, 101}})});
}

TEST(ExtractorFlat, HeldRunEmitsNothing) {
    expectAgrees({makeRow("t0", {{1, 100}}), makeRow("t1", {{1, 100}}),
                  makeRow("t2", {{1, 100}}), makeRow("t3", {{1, 100}})});
}

TEST(ExtractorFlat, AggregatedDeltaGreaterThanOne) {
    expectAgrees({makeRow("t0", {{1, 100}}), makeRow("t1", {{1, 105}})});
}

TEST(ExtractorFlat, CounterReset) {
    expectAgrees({makeRow("t0", {{1, 900}}), makeRow("t1", {{1, 3}})});
}

TEST(ExtractorFlat, ResetThenAdvance) {
    expectAgrees({makeRow("t0", {{1, 900}}), makeRow("t1", {{1, 3}}),
                  makeRow("t2", {{1, 4}}),   makeRow("t3", {{1, 4}}),
                  makeRow("t4", {{1, 9}})});
}

TEST(ExtractorFlat, AllHeadsFireSimultaneously) {
    mas::RawRow r0, r1;
    r0.ts = "t0"; r1.ts = "t1";
    for (int h = 0; h < mas::NUM_HEADS; ++h) {
        r0.count[h] = 10; r0.torque[h] = 2.5; r0.status[h] = 0;
        r1.count[h] = 11; r1.torque[h] = 2.5; r1.status[h] = 0;
    }
    expectAgrees({r0, r1});
}

TEST(ExtractorFlat, RejectAndNoLoadStatusesAgree) {
    // status 65 = Bad Closure (reject), 9 = No InTorque (reject), 2 = No Load,
    // 4 = No Closure (not a reject). is_fault must match on every one.
    expectAgrees({makeRow("t0", {{2, 50}}, 2.0, 0.0),
                  makeRow("t1", {{2, 51}}, 1.9, 65.0),
                  makeRow("t2", {{2, 52}}, 1.8, 9.0),
                  makeRow("t3", {{2, 53}}, 0.0, 2.0),
                  makeRow("t4", {{2, 54}}, 0.5, 4.0)});
}

TEST(ExtractorFlat, HeaderIsRejectedWhenTheColumnCountIsWrong) {
    const std::string p = mas::test::temp_artifact("flat_bad_header.csv");
    { std::ofstream f(p); f << "timestamp,nope,alsonope\n1,2,3\n"; }
    mas::RawColumns cols; std::string err;
    EXPECT_FALSE(mas::load_columns(p, cols, err));
    EXPECT_NE(err.find("has 3 columns"), std::string::npos) << "error was: " << err;
    std::remove(p.c_str());
}

// The count check short-circuits the name check, so a wrong *name* needs a
// header of the right width to reach it. Both branches must name what is wrong.
TEST(ExtractorFlat, HeaderIsRejectedWhenAColumnNameIsWrong) {
    const std::string p = mas::test::temp_artifact("flat_bad_name.csv");
    {
        std::ofstream f(p);
        auto hdr = mas::expected_header();
        hdr[1] = "nope";
        for (std::size_t i = 0; i < hdr.size(); ++i) f << (i ? "," : "") << hdr[i];
        f << "\n";
    }
    mas::RawColumns cols; std::string err;
    EXPECT_FALSE(mas::load_columns(p, cols, err));
    EXPECT_NE(err.find("column 1"), std::string::npos) << "error was: " << err;
    EXPECT_NE(err.find("nope"), std::string::npos) << "error was: " << err;
    std::remove(p.c_str());
}

TEST(ExtractorFlat, MissingFileIsReported) {
    mas::RawColumns cols; std::string err;
    EXPECT_FALSE(mas::load_columns(mas::test::temp_artifact("definitely_not_here.csv"), cols, err));
    EXPECT_FALSE(err.empty());
}

// Helper: write a 2-row CSV with the real header, using `eol` as the terminator.
namespace {
void writeTiny(const std::string& path, const std::string& eol) {
    std::ofstream f(path, std::ios::binary);
    const auto hdr = mas::expected_header();
    for (std::size_t i = 0; i < hdr.size(); ++i) f << (i ? "," : "") << hdr[i];
    f << eol;
    for (int row = 0; row < 2; ++row) {
        f << "2026-02-01T00:00:0" << row << ".000";
        for (int h = 0; h < mas::NUM_HEADS; ++h) f << "," << (100 + row);
        for (int h = 0; h < mas::NUM_HEADS; ++h) f << ",2.54";
        for (int h = 0; h < mas::NUM_HEADS; ++h) f << ",0";
        f << eol;
    }
}

// Anchored on this source file, not the CWD: gtest_discover_tests runs every
// test from the build directory, where a repo-root-relative pool path never
// resolves. Same convention as python/tests/test_clean_vectorized.py.
std::string repo_path(const std::string& rel) {
    return (std::filesystem::path(__FILE__).parent_path().parent_path() / rel)
        .string();
}

} // namespace

TEST(ExtractorFlat, CrlfYieldsIdenticalEventsToLf) {
    writeTiny(mas::test::temp_artifact("flat_lf.csv"), "\n");
    writeTiny(mas::test::temp_artifact("flat_crlf.csv"), "\r\n");
    mas::RawColumns a, b; std::string err;
    ASSERT_TRUE(mas::load_columns(mas::test::temp_artifact("flat_lf.csv"), a, err)) << err;
    ASSERT_TRUE(mas::load_columns(mas::test::temp_artifact("flat_crlf.csv"), b, err)) << err;
    std::vector<mas::CapEvent> ea, eb;
    mas::extract_flat(a.ts, a.count.data(), a.torque.data(), a.status.data(), a.n_rows, ea);
    mas::extract_flat(b.ts, b.count.data(), b.torque.data(), b.status.data(), b.n_rows, eb);
    ASSERT_EQ(ea.size(), static_cast<std::size_t>(mas::NUM_HEADS));
    expectSame(ea, eb);
    std::remove(mas::test::temp_artifact("flat_lf.csv").c_str()); std::remove(mas::test::temp_artifact("flat_crlf.csv").c_str());
}

// The real-data gate. Skipped when the pool has not been extracted.
TEST(ExtractorFlat, AgreesWithStatefulExtractorOnARealDayFile) {
    const std::string p = repo_path(
        "telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02/"
        "telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02-01.csv");
    std::ifstream probe(p);
    if (!probe.good()) GTEST_SKIP() << "pool not extracted: " << p;
    probe.close();

    mas::RawColumns cols; std::string err;
    ASSERT_TRUE(mas::load_columns(p, cols, err)) << err;

    std::vector<mas::RawRow> rows(cols.n_rows);
    for (std::size_t i = 0; i < cols.n_rows; ++i) {
        rows[i].ts = cols.ts[i];
        for (int h = 0; h < mas::NUM_HEADS; ++h) {
            rows[i].count[h]  = cols.count[i * mas::NUM_HEADS + h];
            rows[i].torque[h] = cols.torque[i * mas::NUM_HEADS + h];
            rows[i].status[h] = cols.status[i * mas::NUM_HEADS + h];
        }
    }
    const auto want = stateful(rows);
    std::vector<mas::CapEvent> got;
    mas::extract_flat(cols.ts, cols.count.data(), cols.torque.data(),
                      cols.status.data(), cols.n_rows, got);
    EXPECT_GT(want.size(), 100000u) << "day-file should yield ~765k events";
    expectSame(want, got);
}

TEST(ExtractorFlat, OverIntJumpSaturatesAndStaysAggregated) {
    // The fabricated-event case CapEvent.hpp documents: counts 100 -> 5e9 once
    // emitted delta=705032604 through a truncating cast, and a truncation
    // landing at <= 1 also cleared `aggregated`. Both extractors and the CUDA
    // kernel now share one saturating policy (DeltaPolicy.hpp).
    const auto out = flat({makeRow("t0", {{1, 100}}),
                           makeRow("t1", {{1, 5000000000LL}})});
    ASSERT_EQ(out.size(), 1u);
    EXPECT_EQ(out[0].delta, INT_MAX);
    EXPECT_TRUE(out[0].aggregated);
    EXPECT_FALSE(out[0].reset);
    expectAgrees({makeRow("t0", {{1, 100}}), makeRow("t1", {{1, 5000000000LL}})});
}

TEST(DeltaPolicy, SaturatedDeltaBoundaries) {
    // Host-compiled unit test of the exact function delta_kernel executes on
    // the device -- the divergence C2 flagged (kernel kept the truncating
    // cast after 9f0f901 fixed both CPU extractors) is now caught on a
    // machine with no CUDA toolchain.
    EXPECT_EQ(mas::saturated_delta(100, 100), 0);                    // held
    EXPECT_EQ(mas::saturated_delta(99, 100), 0);                     // reset
    EXPECT_EQ(mas::saturated_delta(101, 100), 1);                    // single cap
    EXPECT_EQ(mas::saturated_delta(105, 100), 5);                    // aggregated
    EXPECT_EQ(mas::saturated_delta(100LL + INT_MAX, 100), INT_MAX);  // exactly at the edge
    EXPECT_EQ(mas::saturated_delta(101LL + INT_MAX, 100), INT_MAX);  // one past: saturate
    EXPECT_EQ(mas::saturated_delta(5000000000LL, 100), INT_MAX);     // the documented case
    EXPECT_EQ(mas::saturated_delta(1LL << 53, -(1LL << 53)), INT_MAX);  // full admitted range
}

TEST(ExtractorFlat, LoadColumnsCountsSkippedRows) {
    // load_columns dropped rows silently and returned true regardless: a
    // corrupt input yielded a short row set the CPU-vs-GPU differential then
    // compared against itself, agreeing perfectly on truncated data. The
    // shared policy now counts them, same as CsvRawReader::skipped().
    namespace fs = std::filesystem;
    const std::string p = mas::test::temp_artifact("t_flat_skipped.csv");
    {
        std::ofstream o(p);
        const auto cols = mas::expected_header();
        for (std::size_t i = 0; i < cols.size(); ++i) o << (i ? "," : "") << cols[i];
        o << "\n";
        auto row = [&o](const std::string& ts, const std::string& torque0) {
            o << ts;
            for (int i = 0; i < 36; ++i) o << ",1.0";
            for (int i = 0; i < 36; ++i) o << (i == 0 ? "," + torque0 : ",2.0");
            for (int i = 0; i < 36; ++i) o << ",0.0";
            o << "\n";
        };
        row("2026-02-01T10:00:00.000", "2.0");
        row("2026-02-01T10:00:01.000", "junk");     // malformed numeric
        row("2026-02-01T10:00:02.000", "1e300");    // out of float range
        row("2026-02-01T10:00:03.000", "2.0");
    }
    mas::RawColumns cols;
    std::string err;
    ASSERT_TRUE(mas::load_columns(p, cols, err)) << err;
    EXPECT_EQ(cols.n_rows, 2u);
    EXPECT_EQ(cols.skipped, 2u);
    fs::remove(p);
}
