#include "mas/store/ParquetEventStore.hpp"
#include <duckdb.hpp>
#include <gtest/gtest.h>
#include <cstdio>
#include <filesystem>
#include <string>
#include <vector>

namespace {

mas::CapEvent ev(int head, const std::string& ts, long long seq, int delta = 1) {
    mas::CapEvent e;
    e.head_id = head;
    e.ts = ts;
    e.cap_seq = seq;
    e.app_torque = 2.0;
    e.status = 2.0;
    e.delta = delta;
    e.is_fault = false;
    e.aggregated = delta > 1;
    e.reset = false;
    return e;
}

// Count rows a glob resolves to, through the same read path the analytics uses.
long long rowsIn(const std::string& glob) {
    duckdb::DuckDB mem(nullptr);
    duckdb::Connection con(mem);
    auto res = con.Query("SELECT COUNT(*) FROM read_parquet('" + glob + "')");
    EXPECT_FALSE(res->HasError()) << res->GetError();
    return res->GetValue(0, 0).GetValue<int64_t>();
}

} // namespace

// A day-file that fails to clean must leave nothing behind. The reader globs
// the directory, so a valid empty Parquet is indistinguishable from a day that
// legitimately produced no events -- and the apps call abandon() for exactly
// this case.
TEST(ParquetEventStore, AbandonWritesNoFileAtAll) {
    const std::string path = "t_pq_abandoned.parquet";
    std::remove(path.c_str());
    {
        mas::ParquetEventStore store(path, "MCC");
        const std::vector<mas::CapEvent> evs{ev(1, "2026-02-01 10:00:00", 100)};
        store.write(evs);
        store.abandon();
        store.close();          // no-op after abandon()
    }                           // destructor must not resurrect the file
    EXPECT_FALSE(std::filesystem::exists(path))
        << "abandon() left a file the reader would treat as a real empty day";
}

TEST(ParquetEventStore, RoundTripsEveryColumn) {
    const std::string p = "t_pq_roundtrip.parquet";
    std::remove(p.c_str());
    {
        mas::ParquetEventStore s(p, "MCC1");
        std::vector<mas::CapEvent> b = {ev(1, "2026-02-01T00:00:01.000", 101),
                                        ev(7, "2026-02-01T00:00:03.000", 900, 3)};
        b[1].app_torque = 1.997;
        b[1].status = 65.0;
        b[1].is_fault = true;
        s.write(b);
        s.close();
    }
    duckdb::DuckDB mem(nullptr);
    duckdb::Connection con(mem);
    auto res = con.Query(
        "SELECT machine_id, head_id, CAST(ts AS VARCHAR), cap_seq, app_torque, "
        "status, delta, is_fault, aggregated, is_reset "
        "FROM read_parquet('" + p + "') WHERE head_id = 7");
    ASSERT_FALSE(res->HasError()) << res->GetError();
    ASSERT_EQ(res->RowCount(), 1u);
    EXPECT_EQ(res->GetValue(0, 0).GetValue<std::string>(), "MCC1");
    EXPECT_EQ(res->GetValue(1, 0).GetValue<int16_t>(), 7);
    EXPECT_EQ(res->GetValue(2, 0).GetValue<std::string>(), "2026-02-01 00:00:03");
    EXPECT_EQ(res->GetValue(3, 0).GetValue<int64_t>(), 900);
    EXPECT_FLOAT_EQ(res->GetValue(4, 0).GetValue<float>(), 1.997f);
    EXPECT_FLOAT_EQ(res->GetValue(5, 0).GetValue<float>(), 65.0f);
    EXPECT_EQ(res->GetValue(6, 0).GetValue<int32_t>(), 3);
    EXPECT_TRUE(res->GetValue(7, 0).GetValue<bool>());
    EXPECT_TRUE(res->GetValue(8, 0).GetValue<bool>());
    EXPECT_FALSE(res->GetValue(9, 0).GetValue<bool>());
    std::remove(p.c_str());
}

TEST(ParquetEventStore, ReprocessingOverwritesTheSameFile) {
    // Idempotency here is a property of the filename, not of a constraint:
    // the same input produces the same path, and the second run replaces it.
    const std::string p = "t_pq_idem.parquet";
    std::remove(p.c_str());
    std::vector<mas::CapEvent> b = {ev(1, "2026-02-01T00:00:01.000", 101),
                                    ev(1, "2026-02-01T00:00:02.000", 102)};
    for (int run = 0; run < 2; ++run) {
        mas::ParquetEventStore s(p, "MCC1");
        s.write(b);
        s.close();
    }
    EXPECT_EQ(rowsIn(p), 2);
    std::remove(p.c_str());
}

TEST(ParquetEventStore, CloseLeavesNoTemporaryBehind) {
    // close() writes through a private name and renames. The temp deliberately
    // does not match *.parquet, but a leaked one is still litter in a directory
    // the reader globs.
    const std::string p = "t_pq_tmp.parquet";
    std::remove(p.c_str());
    {
        mas::ParquetEventStore s(p, "MCC1");
        s.write(std::vector<mas::CapEvent>{ev(1, "2026-02-01T00:00:01.000", 101)});
        s.close();
    }
    for (const auto& e : std::filesystem::directory_iterator("."))
        EXPECT_EQ(e.path().filename().string().find("t_pq_tmp.parquet.tmp."), std::string::npos)
            << "close() left " << e.path().filename().string();
    EXPECT_EQ(rowsIn(p), 1);
    std::remove(p.c_str());
}

TEST(ParquetEventStore, TwoWritersOnOnePathLeaveOneWholeFile) {
    // The re-dispatch case, minus the concurrency a unit test cannot stage: a
    // tombstoned worker and its replacement both derive this path from the same
    // input basename. The closes here are sequential -- a unit test cannot stage
    // the real interleaving -- so what this pins down is the property that makes
    // the interleaving survivable: each store writes its own temp and renames,
    // so the second replaces the first's file whole and the reader sees one
    // writer's full row count, never a concatenation or a partial.
    const std::string p = "t_pq_race.parquet";
    std::remove(p.c_str());
    {
        mas::ParquetEventStore a(p, "MCC1");
        mas::ParquetEventStore b(p, "MCC1");
        a.write(std::vector<mas::CapEvent>{ev(1, "2026-02-01T00:00:01.000", 101),
                                           ev(1, "2026-02-01T00:00:02.000", 102)});
        b.write(std::vector<mas::CapEvent>{ev(1, "2026-02-01T00:00:01.000", 101),
                                           ev(1, "2026-02-01T00:00:02.000", 102)});
        a.close();
        b.close();
    }
    EXPECT_EQ(rowsIn(p), 2) << "a torn or concatenated file, not one writer's copy";
    std::remove(p.c_str());
}

TEST(ParquetEventStore, AFailedWriteWritesNoFileAtAll) {
    // The reachable case: CsvRawReader copies the timestamp cell verbatim (only
    // the numeric cells go through stod and the skipped_ counter), so a corrupt
    // one reaches the VARCHAR -> TIMESTAMP cast here and throws. What used to
    // happen was worse than the throw: the destructor wrote out whatever had
    // already been flushed, leaving a partial day that reads as a whole one --
    // and buffering one Appender for the store's life widened that window from
    // a batch to 204,800 rows.
    const std::string path = "t_pq_failed.parquet";
    std::remove(path.c_str());
    {
        mas::ParquetEventStore store(path, "MCC");
        store.write(std::vector<mas::CapEvent>{ev(1, "2026-02-01 10:00:00", 1)});
        EXPECT_THROW(store.write(std::vector<mas::CapEvent>{ev(1, "GARBAGE-TS", 2)}),
                     std::exception);
        // close() must refuse rather than write the rows that survived...
        EXPECT_THROW(store.close(), std::runtime_error);
    }   // ...and the destructor must not write them either.
    EXPECT_FALSE(std::filesystem::exists(path))
        << "a failed clean left a file the reader would treat as a real day";
    for (const auto& e : std::filesystem::directory_iterator("."))
        EXPECT_EQ(e.path().filename().string().find("t_pq_failed.parquet.tmp."),
                  std::string::npos)
            << "left a temp behind: " << e.path().filename().string();
}

TEST(ParquetEventStore, EmptyInputWritesAReadableFile) {
    // A day-file that yields no events must still leave something a glob can
    // read, or every later read_parquet('*.parquet') fails on its account.
    const std::string p = "t_pq_empty.parquet";
    std::remove(p.c_str());
    {
        mas::ParquetEventStore s(p, "MCC1");
        s.close();
    }
    EXPECT_EQ(rowsIn(p), 0);
    std::remove(p.c_str());
}

TEST(ParquetEventStore, PathContainingASingleQuoteWorks) {
    const std::string p = "t_pq_o'brien.parquet";
    std::remove(p.c_str());
    {
        mas::ParquetEventStore s(p, "MCC1");
        std::vector<mas::CapEvent> b = {ev(1, "2026-02-01T00:00:01.000", 101)};
        s.write(b);
        EXPECT_NO_THROW(s.close());
    }
    EXPECT_EQ(rowsIn("t_pq_o''brien.parquet"), 1);
    std::remove(p.c_str());
}

TEST(ParquetEventStore, CountReportsEventsAccepted) {
    const std::string p = "t_pq_count.parquet";
    std::remove(p.c_str());
    mas::ParquetEventStore s(p, "MCC1");
    std::vector<mas::CapEvent> b = {ev(1, "2026-02-01T00:00:01.000", 101),
                                    ev(1, "2026-02-01T00:00:02.000", 102)};
    s.write(b);
    s.write(b);                       // buffered, not deduplicated: no constraint here
    EXPECT_EQ(s.count(), 4);
    s.close();
    std::remove(p.c_str());
}
