import pytest
from analytics.store import connect, discover_heads, period_clause
from analytics.result import ToolResult


def test_discovers_heads_from_data_not_a_hard_coded_36(tiny_cfg):
    con = connect(tiny_cfg)
    assert discover_heads(con) == [1, 2, 3]     # NOT range(1, 37)


def test_store_is_read_only(tiny_cfg):
    con = connect(tiny_cfg)
    with pytest.raises(Exception):
        con.execute("DELETE FROM cap_events")


def test_period_clause_single_month():
    sql, params = period_clause("2026-02")
    assert "ts >= ?" in sql and "ts < ?" in sql
    assert params == ["2026-02-01", "2026-03-01"]


def test_period_clause_range():
    sql, params = period_clause("2026-02..2026-04")
    assert params == ["2026-02-01", "2026-05-01"]


def test_period_clause_none_matches_everything():
    sql, params = period_clause(None)
    assert sql == "TRUE"
    assert params == []


def test_period_clause_rejects_garbage():
    with pytest.raises(ValueError, match="unparseable period"):
        period_clause("last tuesday")


def test_result_helpers_carry_status():
    r = ToolResult.insufficient("overview", "no rows in period", period="2026-09")
    assert r.status == "insufficient_data"
    assert r.values == {}
    assert r.provenance.period == "2026-09"
