"""closure_filter: the brief's filtering queries, counted exactly and listed.

The tiny store (conftest) has six capping operations: head 1 at 2.00, 2.10 and
1.90 (all successful), head 2 at 2.00 (successful), 1.99 (status 65, Bad Closure)
and 1.95 (status 9, No InTorque), and two no-load cycles of head 3 at torque 0.
"""
import pytest

import analytics.tools.filter as flt
from analytics.tools.filter import closure_filter


def _f(cfg, **kw):
    return closure_filter(cfg, period="2026-02", **kw)


def test_a_lower_limit_counts_the_closures_strictly_above_it(tiny_cfg):
    r = _f(tiny_cfg, above=1.99)
    v = r.values
    assert r.status == "ok"
    assert v["count"] == 3                       # 2.00, 2.10 and 2.00; 1.99 itself is out
    assert v["by_head"] == [{"head_id": 1, "count": 2}, {"head_id": 2, "count": 1}]
    assert v["capping_operations"] == 6


def test_an_upper_limit_is_strict_too(tiny_cfg):
    assert _f(tiny_cfg, below=1.95).values["count"] == 1      # 1.90 only


def test_no_load_cycles_never_match_a_low_limit(tiny_cfg):
    # two cycles at torque 0 would match "below 1.0" if they counted
    assert _f(tiny_cfg, below=1.0).values["count"] == 0


def test_failed_closures_are_listed_with_what_rejected_them(tiny_cfg):
    v = _f(tiny_cfg, outcome="failed").values
    assert v["count"] == 2
    assert [e["outcome"] for e in v["events"]] == ["rejected: Bad Closure",
                                                   "rejected: No InTorque"]
    assert {e["head_id"] for e in v["events"]} == {2}


def test_one_head_and_one_outcome_together(tiny_cfg):
    assert _f(tiny_cfg, head=2, outcome="failed").values["count"] == 2
    assert _f(tiny_cfg, head=2, outcome="successful").values["count"] == 1
    assert _f(tiny_cfg, head=1, outcome="failed").values["count"] == 0


def test_a_filter_that_matches_nothing_is_a_zero_not_a_gap(tiny_cfg):
    r = _f(tiny_cfg, above=2.5)
    assert r.status == "ok" and r.values["count"] == 0 and r.values["events"] == []


def test_the_events_are_capped_but_the_count_is_exact(tiny_cfg, monkeypatch):
    monkeypatch.setattr(flt, "LISTED", 2)
    v = _f(tiny_cfg).values
    assert v["count"] == 6 and v["listed"] == 2 and len(v["events"]) == 2


def test_the_events_come_in_time_order(tiny_cfg):
    stamps = [str(e["ts"]) for e in _f(tiny_cfg).values["events"]]
    assert stamps == sorted(stamps)


def test_a_period_without_data_is_insufficient(tiny_cfg):
    assert closure_filter(tiny_cfg, period="2026-06", above=2.0).status == "insufficient_data"


@pytest.mark.parametrize("kw", [{"outcome": "weird"}, {"head": 0}])
def test_bad_arguments_are_errors_not_crashes(tiny_cfg, kw):
    assert _f(tiny_cfg, **kw).status == "error"


def test_the_limit_is_compared_as_the_real_the_store_holds(tmp_path):
    # 2.2 stored as REAL is 2.2000000476...; against the double 2.2 it would be
    # "above" itself.
    import duckdb
    from analytics.config import Config
    from tests.conftest import CAP_EVENTS_DDL
    path = tmp_path / "edge.duckdb"
    con = duckdb.connect(str(path))
    con.execute(CAP_EVENTS_DDL)
    con.execute("INSERT INTO cap_events VALUES ('MCC',1,'2026-02-01 00:00:00',1,2.2,0.0,1,false,false,false)")
    con.close()
    r = closure_filter(Config(store_path=str(path)), period="2026-02", above=2.2)
    assert r.values["count"] == 0
