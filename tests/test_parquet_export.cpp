#include "mas/store/DuckDbEventStore.hpp"
#include "mas/store/ParquetExport.hpp"
#include "mas/store/SqlQuote.hpp"
#include <duckdb.hpp>
#include <gtest/gtest.h>
#include <filesystem>
#include <string>
#include <vector>

namespace {

namespace fs = std::filesystem;

// machine_id is not on CapEvent: the store labels rows with the id it was
// opened with, which is why every row below comes back as "MCC".
mas::CapEvent ev(int head, const char* ts, long long seq, double torque) {
    mas::CapEvent e;
    e.head_id = head;
    e.ts = ts;
    e.cap_seq = seq;
    e.app_torque = torque;
    e.status = 0.0;
    e.delta = 1;
    e.is_fault = false;
    e.aggregated = false;
    e.reset = false;
    return e;
}

// A store with three events on two heads, spread across three days.
std::string makeStore(const std::string& path) {
    fs::remove(path);
    fs::remove(path + ".wal");
    mas::DuckDbEventStore s(path, "MCC");
    const std::vector<mas::CapEvent> evs{
        ev(1, "2026-02-01 10:00:00", 100, 1.5),
        ev(2, "2026-02-02 11:00:00", 101, 2.5),
        ev(1, "2026-02-03 12:00:00", 102, 3.5)};
    s.write(evs);
    return path;
}

long long parquetCount(const std::string& pq) {
    duckdb::DuckDB db(nullptr);
    duckdb::Connection con(db);
    auto res = con.Query("SELECT COUNT(*) FROM read_parquet('" +
                         mas::sql_quote(pq) + "')");
    EXPECT_FALSE(res->HasError()) << res->GetError();
    return res->GetValue(0, 0).GetValue<int64_t>();
}

class ParquetExportTest : public ::testing::Test {
protected:
    void SetUp() override {
        dir_ = fs::temp_directory_path() / "mas_export_test";
        fs::remove_all(dir_);
        fs::create_directories(dir_);
    }
    void TearDown() override {
        std::error_code ec;
        fs::permissions(dir_, fs::perms::owner_all, fs::perm_options::add, ec);
        for (const auto& e : fs::recursive_directory_iterator(dir_))
            fs::permissions(e.path(), fs::perms::owner_all, fs::perm_options::add, ec);
        fs::remove_all(dir_, ec);
    }
    std::string p(const char* name) const { return (dir_ / name).string(); }
    fs::path dir_;
};

TEST_F(ParquetExportTest, ExportsEveryRowAndEveryColumn) {
    const auto db = makeStore(p("s.duckdb"));
    const auto pq = p("out.parquet");

    const auto r = mas::export_store_to_parquet(db, pq);
    EXPECT_EQ(r.rows, 3);
    EXPECT_EQ(r.path, pq);
    ASSERT_TRUE(fs::exists(pq));

    duckdb::DuckDB mem(nullptr);
    duckdb::Connection con(mem);
    auto res = con.Query(
        "SELECT machine_id, head_id, ts, cap_seq, app_torque, status, delta, "
        "is_fault, aggregated, is_reset FROM read_parquet('" + pq + "') "
        "ORDER BY head_id, ts");
    ASSERT_FALSE(res->HasError()) << res->GetError();
    ASSERT_EQ(res->RowCount(), 3u);
    // ORDER BY head_id, ts: (1, 02-01), (1, 02-03), (2, 02-02)
    EXPECT_EQ(res->GetValue(0, 0).ToString(), "MCC");
    EXPECT_EQ(res->GetValue(1, 0).GetValue<int16_t>(), 1);
    EXPECT_EQ(res->GetValue(3, 0).GetValue<int64_t>(), 100);
    EXPECT_FLOAT_EQ(res->GetValue(4, 0).GetValue<float>(), 1.5f);
    EXPECT_EQ(res->GetValue(3, 1).GetValue<int64_t>(), 102);
    EXPECT_EQ(res->GetValue(1, 2).GetValue<int16_t>(), 2);
    EXPECT_EQ(res->GetValue(3, 2).GetValue<int64_t>(), 101);
}

// The requirement that made this a separate function instead of a method on
// DuckDbEventStore: that constructor creates tables and writes store_meta, so
// routing an export through it would make the exporter a writer. A store the
// filesystem refuses to let anyone write is the only honest way to test it.
TEST_F(ParquetExportTest, DoesNotWriteToAReadOnlyStore) {
    const auto db = makeStore(p("ro.duckdb"));
    ASSERT_FALSE(fs::exists(db + ".wal")) << "store must be checkpointed first";

    const auto before_size = fs::file_size(db);
    const auto before_time = fs::last_write_time(db);

    std::error_code ec;
    fs::permissions(db, fs::perms::owner_write | fs::perms::group_write |
                        fs::perms::others_write,
                    fs::perm_options::remove, ec);
    ASSERT_FALSE(ec) << ec.message();

    const auto r = mas::export_store_to_parquet(db, p("ro.parquet"));
    EXPECT_EQ(r.rows, 3);

    EXPECT_EQ(fs::file_size(db), before_size);
    EXPECT_EQ(fs::last_write_time(db), before_time);
    EXPECT_FALSE(fs::exists(db + ".wal"))
        << "a read-only export must not leave a write-ahead log behind";
}

TEST_F(ParquetExportTest, SinceAndUntilBoundInclusively) {
    const auto db = makeStore(p("range.duckdb"));

    EXPECT_EQ(mas::export_store_to_parquet(db, p("a.parquet"), "2026-02-02").rows, 2);
    EXPECT_EQ(mas::export_store_to_parquet(db, p("b.parquet"), "", "2026-02-02").rows, 2);
    EXPECT_EQ(mas::export_store_to_parquet(db, p("c.parquet"),
                                           "2026-02-02", "2026-02-02 23:59:59").rows, 1);
    // Inclusive on both ends: the exact timestamp of a row is inside its own range.
    EXPECT_EQ(mas::export_store_to_parquet(db, p("d.parquet"),
                                           "2026-02-01 10:00:00",
                                           "2026-02-01 10:00:00").rows, 1);
}

// A bare date as the upper bound covers that whole day. Read literally it is
// midnight, which would drop the day's rows while the export still succeeded
// and still printed a count -- silently, which is the failure this guards.
TEST_F(ParquetExportTest, ABareUpperDateCoversTheWholeDay) {
    const auto db = makeStore(p("day.duckdb"));

    // The 02-02 row is at 11:00. Midnight semantics would return 1 here.
    EXPECT_EQ(mas::export_store_to_parquet(db, p("whole.parquet"), "", "2026-02-02").rows, 2);

    // An explicit time is not widened: 10:00 is before the 11:00 row.
    EXPECT_EQ(mas::export_store_to_parquet(db, p("exact.parquet"), "",
                                           "2026-02-02 10:00:00").rows, 1);
}

// A glob over an export directory must not fail because one slice happened to
// match nothing, so an empty result is a valid file, not an error.
TEST_F(ParquetExportTest, EmptyRangeWritesAReadableEmptyFile) {
    const auto db = makeStore(p("empty.duckdb"));
    const auto pq = p("none.parquet");

    const auto r = mas::export_store_to_parquet(db, pq, "2027-01-01");
    EXPECT_EQ(r.rows, 0);
    ASSERT_TRUE(fs::exists(pq));
    EXPECT_EQ(parquetCount(pq), 0);
}

TEST_F(ParquetExportTest, CreatesTheDestinationDirectory) {
    const auto db = makeStore(p("mk.duckdb"));
    const auto pq = (dir_ / "nested" / "deeper" / "out.parquet").string();
    EXPECT_EQ(mas::export_store_to_parquet(db, pq).rows, 3);
    EXPECT_TRUE(fs::exists(pq));
}

TEST_F(ParquetExportTest, HandlesAQuoteInEitherPath) {
    const auto db = makeStore(p("o'brien.duckdb"));
    const auto pq = p("o'brien.parquet");
    EXPECT_EQ(mas::export_store_to_parquet(db, pq).rows, 3);
    EXPECT_EQ(parquetCount(pq), 3);
}

// COPY ... TO truncates whatever it is pointed at. Pointed at the store, it
// replaced a 536 KB DuckDB database with a 1.7 KB Parquet, then read that
// Parquet back, agreed with itself on the row count, and exited 0.
TEST_F(ParquetExportTest, RefusesToExportAStoreOntoItself) {
    const auto db = makeStore(p("self.duckdb"));
    const auto before = fs::file_size(db);

    EXPECT_THROW(mas::export_store_to_parquet(db, db), std::runtime_error);
    EXPECT_EQ(fs::file_size(db), before) << "the store was overwritten";

    // Still a database, still readable, still holding its rows.
    duckdb::DuckDB reopened(db);
    duckdb::Connection con(reopened);
    auto res = con.Query("SELECT COUNT(*) FROM cap_events");
    ASSERT_FALSE(res->HasError()) << res->GetError();
    EXPECT_EQ(res->GetValue(0, 0).GetValue<int64_t>(), 3);
}

TEST_F(ParquetExportTest, RefusesToOverwriteAnExistingDestination) {
    const auto db = makeStore(p("ow.duckdb"));
    const auto pq = p("taken.parquet");
    ASSERT_EQ(mas::export_store_to_parquet(db, pq).rows, 3);
    const auto first = fs::file_size(pq);

    EXPECT_THROW(mas::export_store_to_parquet(db, pq), std::runtime_error);
    EXPECT_EQ(fs::file_size(pq), first);
}

TEST_F(ParquetExportTest, RefusesToWriteTheStoresWriteAheadLog) {
    // The two guards above both key on the destination already existing, and a
    // WAL that DuckDB has not created yet does not. mas_merge documents a WAL
    // precondition, so a Parquet file sitting at that name is a trap for
    // whoever reaches it.
    const auto db = makeStore(p("wal.duckdb"));
    const auto wal = db + ".wal";
    ASSERT_FALSE(fs::exists(wal)) << "fixture already has a WAL; the case is different";

    EXPECT_THROW(mas::export_store_to_parquet(db, wal), std::runtime_error);
    EXPECT_FALSE(fs::exists(wal)) << "a Parquet file was left where the log goes";
}

TEST_F(ParquetExportTest, MissingStoreFailsByName) {
    try {
        mas::export_store_to_parquet(p("nope.duckdb"), p("x.parquet"));
        FAIL() << "expected a throw for a store that does not exist";
    } catch (const std::runtime_error& e) {
        EXPECT_NE(std::string(e.what()).find("nope.duckdb"), std::string::npos)
            << "error was: " << e.what();
    }
    EXPECT_FALSE(fs::exists(p("x.parquet")))
        << "a failed export must not leave a file behind";
}

} // namespace
