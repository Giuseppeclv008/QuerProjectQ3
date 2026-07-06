#include <duckdb.hpp>
#include <gtest/gtest.h>

namespace {

TEST(DuckDbSmoke, OpensInMemoryAndSelects42) {
    duckdb::DuckDB db(nullptr);        // in-memory database
    duckdb::Connection con(db);
    auto res = con.Query("SELECT 42");
    ASSERT_FALSE(res->HasError()) << res->GetError();
    EXPECT_EQ(res->GetValue(0, 0).GetValue<int>(), 42);
}

} // namespace
