#include "mas/DuckDbEventStore.hpp"
#include <duckdb.hpp>
#include <gtest/gtest.h>
#include <cstdio>
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

void removeDb(const std::string& path) {
    std::remove(path.c_str());
    std::remove((path + ".wal").c_str());
}

TEST(DuckDbEventStore, WritePersistsRowsWithTypedColumns) {
    const std::string path = "t_store_basic.duckdb";
    removeDb(path);
    {
        mas::DuckDbEventStore store(path, "MCC1");
        std::vector<mas::CapEvent> batch = {
            ev(1, "2026-02-01T00:00:01.000", 101),
            ev(2, "2026-02-01T00:00:02.000", 55, 3),
        };
        store.write(batch);
        EXPECT_EQ(store.count(), 2);
    }   // store closed — safe to reopen the file directly
    duckdb::DuckDB db(path);
    duckdb::Connection con(db);
    auto res = con.Query(
        "SELECT machine_id, head_id, CAST(ts AS VARCHAR), cap_seq, delta, aggregated "
        "FROM cap_events WHERE head_id = 2");
    ASSERT_FALSE(res->HasError()) << res->GetError();
    ASSERT_EQ(res->RowCount(), 1u);
    EXPECT_EQ(res->GetValue(0, 0).GetValue<std::string>(), "MCC1");
    EXPECT_EQ(res->GetValue(1, 0).GetValue<int16_t>(), 2);
    EXPECT_EQ(res->GetValue(2, 0).GetValue<std::string>(), "2026-02-01 00:00:02");
    EXPECT_EQ(res->GetValue(3, 0).GetValue<int64_t>(), 55);
    EXPECT_EQ(res->GetValue(4, 0).GetValue<int32_t>(), 3);
    EXPECT_EQ(res->GetValue(5, 0).GetValue<bool>(), true);
    removeDb(path);
}

TEST(DuckDbEventStore, RewritingSameBatchIsIdempotent) {
    const std::string path = "t_store_idem.duckdb";
    removeDb(path);
    mas::DuckDbEventStore store(path, "MCC1");
    std::vector<mas::CapEvent> batch = {
        ev(1, "2026-02-01T00:00:01.000", 101),
        ev(1, "2026-02-01T00:00:05.000", 102),
    };
    store.write(batch);
    store.write(batch);                    // reprocess: must not duplicate (spec §10)
    EXPECT_EQ(store.count(), 2);
    removeDb(path);
}

TEST(DuckDbEventStore, DuplicateKeyWithinOneBatchIsIgnored) {
    const std::string path = "t_store_dup.duckdb";
    removeDb(path);
    mas::DuckDbEventStore store(path, "MCC1");
    std::vector<mas::CapEvent> batch = {
        ev(1, "2026-02-01T00:00:01.000", 101),
        ev(1, "2026-02-01T00:00:01.000", 101),   // same (machine, head, cap_seq)
    };
    store.write(batch);
    EXPECT_EQ(store.count(), 1);
    removeDb(path);
}

TEST(DuckDbEventStore, ReopenKeepsRowsAndUpsertsAcrossRuns) {
    const std::string path = "t_store_reopen.duckdb";
    removeDb(path);
    {
        mas::DuckDbEventStore store(path, "MCC1");
        std::vector<mas::CapEvent> b1 = {ev(1, "2026-02-01T00:00:01.000", 101)};
        store.write(b1);
    }
    {
        mas::DuckDbEventStore store(path, "MCC1");
        std::vector<mas::CapEvent> b2 = {
            ev(1, "2026-02-01T00:00:01.000", 101),   // already stored
            ev(1, "2026-02-01T00:00:09.000", 102),   // new
        };
        store.write(b2);
        EXPECT_EQ(store.count(), 2);
    }
    removeDb(path);
}

TEST(DuckDbEventStore, ExportParquetRoundtrips) {
    const std::string path = "t_store_parquet.duckdb";
    const std::string pq = "t_store_events.parquet";
    removeDb(path);
    std::remove(pq.c_str());

    mas::DuckDbEventStore store(path, "MCC1");
    std::vector<mas::CapEvent> batch = {
        ev(1, "2026-02-01T00:00:01.000", 101),
        ev(1, "2026-02-01T00:00:05.000", 102),
        ev(7, "2026-02-01T00:00:03.000", 900, 2),
    };
    store.write(batch);
    store.export_parquet(pq);

    duckdb::DuckDB mem(nullptr);
    duckdb::Connection con(mem);
    auto res = con.Query("SELECT COUNT(*), MIN(head_id), MAX(cap_seq) FROM read_parquet('" + pq + "')");
    ASSERT_FALSE(res->HasError()) << res->GetError();
    EXPECT_EQ(res->GetValue(0, 0).GetValue<int64_t>(), 3);
    EXPECT_EQ(res->GetValue(1, 0).GetValue<int16_t>(), 1);
    EXPECT_EQ(res->GetValue(2, 0).GetValue<int64_t>(), 900);

    std::remove(pq.c_str());
    removeDb(path);
}

} // namespace
