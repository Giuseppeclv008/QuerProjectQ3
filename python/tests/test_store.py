import pytest
from analytics.store import connect, discover_heads, period_clause, scope_clause
from analytics.result import ToolResult
from analytics.config import Config


def test_discovers_heads_from_data_not_a_hard_coded_36(tiny_cfg):
    con = connect(tiny_cfg)
    assert discover_heads(con, tiny_cfg) == [1, 2, 3]     # NOT range(1, 37)


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


def test_ok_carries_values_and_full_provenance():
    r = ToolResult.ok(
        "overview", {"n": 5},
        period="2026-02", rows_scanned=100,
        filters=["torque_band=1.5..2.5"], assumptions=["idle=300s"],
    )
    assert r.status == "ok"
    assert r.values == {"n": 5}
    assert r.provenance.period == "2026-02"
    assert r.provenance.rows_scanned == 100
    assert r.provenance.filters == ["torque_band=1.5..2.5"]
    assert r.provenance.assumptions == ["idle=300s"]


def test_error_carries_status_message_and_provenance():
    r = ToolResult.error(
        "overview", "boom",
        period="2026-02", rows_scanned=10,
        filters=["torque_band=1.5..2.5"], assumptions=["idle=300s"],
    )
    assert r.status == "error"
    assert r.message == "boom"
    assert r.values == {}
    assert r.provenance.period == "2026-02"
    assert r.provenance.rows_scanned == 10
    assert r.provenance.filters == ["torque_band=1.5..2.5"]
    assert r.provenance.assumptions == ["idle=300s"]


def test_insufficient_carries_filters_and_assumptions():
    r = ToolResult.insufficient(
        "overview", "no rows in period",
        period="2026-09", rows_scanned=3,
        filters=["torque_band=1.5..2.5"], assumptions=["idle=300s"],
    )
    assert r.status == "insufficient_data"
    assert r.provenance.filters == ["torque_band=1.5..2.5"]
    assert r.provenance.assumptions == ["idle=300s"]


def test_scope_clause_scopes_machine_and_period(tiny_cfg):
    where, params = scope_clause(tiny_cfg, "2026-02")
    assert "machine_id = ?" in where
    assert params == ["MCC", "2026-02-01", "2026-03-01"]


def test_discover_heads_empty_for_unknown_machine(tiny_store):
    cfg = Config(store_path=tiny_store, machine_id="NOPE")
    con = connect(cfg)
    assert discover_heads(con, cfg) == []
