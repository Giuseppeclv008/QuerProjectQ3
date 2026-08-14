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

} // namespace

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
