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

TEST(DuckDbEventStore, MergeFromUnionsStoresIdempotently) {
    const std::string a = "t_merge_a.duckdb";
    const std::string b = "t_merge_b.duckdb";
    const std::string dst = "t_merge_dst.duckdb";
    removeDb(a); removeDb(b); removeDb(dst);
    {
        mas::DuckDbEventStore sa(a, "MCC1");
        std::vector<mas::CapEvent> ba = {
            ev(1, "2026-02-01T00:00:01.000", 101),
            ev(1, "2026-02-01T00:00:02.000", 102),
        };
        sa.write(ba);
        mas::DuckDbEventStore sb(b, "MCC1");
        std::vector<mas::CapEvent> bb = {
            ev(1, "2026-02-01T00:00:02.000", 102),   // overlaps store a
            ev(2, "2026-02-01T00:00:03.000", 55),
        };
        sb.write(bb);
    }   // close source stores before attaching them read-only

    mas::DuckDbEventStore d(dst, "MCC1");
    d.merge_from(a);
    d.merge_from(b);
    EXPECT_EQ(d.count(), 3);   // union: (1,101), (1,102), (2,55)
    d.merge_from(b);           // re-merge is safe (spec §10 idempotency)
    EXPECT_EQ(d.count(), 3);
    removeDb(a); removeDb(b); removeDb(dst);
}

// --- Deferred from Plan 2's final review ---

TEST(DuckDbEventStore, ConstructorThrowsWhenParentDirectoryMissing) {
    // The ctor's open-failure path was untested. A path inside a directory
    // that doesn't exist can't be created by DuckDB, so the ctor should
    // wrap the underlying failure (see DuckDbEventStore.hpp: "Throws
    // std::runtime_error if the database cannot be opened.").
    EXPECT_THROW(
        mas::DuckDbEventStore store("no_such_dir_for_ctor_test/foo.duckdb", "MCC1"),
        std::runtime_error);
}

TEST(DuckDbEventStore, WriteHandlesBatchLargerThanPipelineKBatchSize) {
    // Deferral note: this was filed as a "kBatch flush-boundary" test on the
    // premise that DuckDbEventStore.cpp has an internal kBatch constant
    // whose flush branch is untested. On inspection, no such constant or
    // branch exists in this file: write() appends the whole input span via
    // one duckdb::Appender session unconditionally, regardless of size.
    // kBatch (=8192) actually lives in Pipeline.cpp, where clean_file()
    // chunks CapEvents before calling store.write() repeatedly — that's a
    // different component. Kept as a regression test at the same scale: it
    // exercises write() with kBatch+1 events in a single call (larger than
    // any existing test), confirming the Appender-based path has no hidden
    // row-count limitation of its own.
    const std::string path = "t_store_large_batch.duckdb";
    removeDb(path);
    mas::DuckDbEventStore store(path, "MCC1");

    constexpr long long kBatch = 8192;   // mirrors Pipeline.cpp's chunk size
    std::vector<mas::CapEvent> batch;
    batch.reserve(static_cast<std::size_t>(kBatch + 1));
    for (long long seq = 0; seq < kBatch + 1; ++seq) {
        batch.push_back(ev(1, "2026-02-01T00:00:01.000", seq));
    }
    store.write(batch);
    EXPECT_EQ(store.count(), kBatch + 1);
    removeDb(path);
}

// --- Final-review fix wave (Plan 4) ---

TEST(DuckDbEventStore, MergeFromDetachesSourceOnInsertFailureSoAliasIsNotPoisoned) {
    // merge_from's INSERT can throw after a successful ATTACH (e.g. a source
    // whose cap_events schema doesn't match the SELECT list). Before this
    // fix that left "src" attached, poisoning every later ATTACH ... AS src
    // in the same connection (mas_merge's skip-and-continue loop hits this).
    const std::string good = "t_merge_detach_good.duckdb";
    const std::string bad = "t_merge_detach_bad.duckdb";
    const std::string dst = "t_merge_detach_dst.duckdb";
    removeDb(good); removeDb(bad); removeDb(dst);
    {
        // Healthy source via the normal ctor/schema.
        mas::DuckDbEventStore sg(good, "MCC1");
        std::vector<mas::CapEvent> bg = {ev(1, "2026-02-01T00:00:01.000", 101)};
        sg.write(bg);
    }
    {
        // Attachable-but-incompatible source: a real DuckDB file (so ATTACH
        // succeeds) whose cap_events table doesn't have the columns
        // merge_from's INSERT ... SELECT names (so the INSERT throws a
        // Binder Error instead of a row ever moving).
        duckdb::DuckDB bad_db(bad);
        duckdb::Connection bad_con(bad_db);
        auto res = bad_con.Query("CREATE TABLE cap_events (only_this_column INTEGER)");
        ASSERT_FALSE(res->HasError()) << res->GetError();
    }   // close before attaching read-only, same convention as the other tests

    mas::DuckDbEventStore d(dst, "MCC1");
    EXPECT_THROW(d.merge_from(bad), std::runtime_error);

    // If the failed merge had left "src" attached, this second merge_from's
    // own ATTACH ... AS src would fail (alias already in use) before its
    // INSERT ever ran. A successful merge here is proof the DETACH on the
    // throw path actually executed.
    EXPECT_NO_THROW(d.merge_from(good));
    EXPECT_EQ(d.count(), 1);

    removeDb(good); removeDb(bad); removeDb(dst);
}

} // namespace
