import duckdb
import pytest

from analytics.config import Config
from analytics.tools.gaps import event_gaps
from tests.conftest import CAP_EVENTS_DDL


@pytest.fixture
def gaps_store(tmp_path):
    """Polls on two heads. The machine-wide gaps (no event from ANY head) are
    00:05 -> 00:30 (1,500 s) and 00:30 -> 02:00 (5,400 s). Head 1 alone is
    silent from 00:01 to 00:30, but head 2 fires at 00:05, so that is not a
    machine gap. One March event sits outside the February period."""
    path = tmp_path / "gaps.duckdb"
    con = duckdb.connect(str(path))
    con.execute(CAP_EVENTS_DDL)
    rows = [
        (1, "2026-02-01 00:00:00", 1, 2.0, 0.0),
        (2, "2026-02-01 00:00:00", 1, 2.0, 0.0),
        (1, "2026-02-01 00:01:00", 2, 2.0, 0.0),
        (2, "2026-02-01 00:05:00", 2, 0.0, 2.0),    # no-load still counts as an event
        (1, "2026-02-01 00:30:00", 3, 2.0, 0.0),
        (2, "2026-02-01 02:00:00", 3, 2.0, 0.0),
        (1, "2026-03-01 00:00:00", 4, 2.0, 0.0),    # outside the period
    ]
    for h, ts, seq, tq, st in rows:
        con.execute("INSERT INTO cap_events VALUES ('MCC',?,?,?,?,?,1,false,false,false)",
                    [h, ts, seq, tq, st])
    con.close()
    return str(path)


def test_finds_the_machine_wide_gaps_only(gaps_store):
    r = event_gaps(Config(store_path=gaps_store), period="2026-02", min_seconds=600)
    assert r.status == "ok"
    v = r.values
    assert [g["duration_seconds"] for g in v["gaps"]] == [1500, 5400]
    assert v["gap_count"] == 2
    assert v["total_gap_seconds"] == 6900
    assert v["total_gap_hours"] == pytest.approx(6900 / 3600)
    assert v["longest_gap"]["duration_seconds"] == 5400
    assert r.provenance.rows_scanned == 6


def test_no_gap_is_attributed_to_a_head(gaps_store):
    r = event_gaps(Config(store_path=gaps_store), period="2026-02", min_seconds=600)
    assert all("head_id" not in g for g in r.values["gaps"])
    assert any("never attributed to a head" in a for a in r.provenance.assumptions)
    assert any("data did not arrive" in a for a in r.provenance.assumptions)


def test_threshold_defaults_to_the_config_max_gap(gaps_store):
    cfg = Config(store_path=gaps_store, idle_max_gap_seconds=3600)
    r = event_gaps(cfg, period="2026-02")
    assert r.values["min_seconds"] == 3600
    assert [g["duration_seconds"] for g in r.values["gaps"]] == [5400]


def test_time_outside_the_events_is_not_a_gap(gaps_store):
    # February ends 26 days after its last event, and March starts with one:
    # neither edge is a gap, and the March event does not close one.
    r = event_gaps(Config(store_path=gaps_store), period="2026-02", min_seconds=1)
    assert r.values["gaps"][-1]["end"].hour == 2


def test_a_period_with_events_but_no_long_gap_is_a_measured_zero(gaps_store):
    r = event_gaps(Config(store_path=gaps_store), period="2026-02", min_seconds=6000)
    assert r.status == "ok"
    assert r.values["gap_count"] == 0
    assert r.values["longest_gap"] is None


def test_an_empty_period_is_insufficient_not_zero(gaps_store):
    r = event_gaps(Config(store_path=gaps_store), period="2026-05")
    assert r.status == "insufficient_data"
