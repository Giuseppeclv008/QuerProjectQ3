import duckdb
import pytest
from analytics.config import Config
from analytics.tools.idle import idle_periods


@pytest.fixture
def idle_store(tmp_path):
    """Head 1: a 60-second sustained no-load run, then a real cap.
       Head 2: two isolated no-load cycles, never sustained."""
    path = tmp_path / "idle.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE cap_events (
            machine_id VARCHAR NOT NULL, head_id SMALLINT NOT NULL, ts TIMESTAMP,
            cap_seq BIGINT NOT NULL, app_torque REAL, status REAL, delta INTEGER,
            is_fault BOOLEAN, aggregated BOOLEAN, is_reset BOOLEAN,
            UNIQUE (machine_id, head_id, ts))
    """)
    rows = []
    for i in range(7):                                   # 0,10,...,60s -> 60s span
        sec = i * 10                                     # i=6 -> 00:01:00 (60s elapsed), a valid literal
        rows.append(("MCC", 1, f"2026-02-01 00:{sec // 60:02d}:{sec % 60:02d}", i + 1, 0.0, 2.0))
    rows.append(("MCC", 1, "2026-02-01 00:02:00", 100, 2.0, 0.0))   # real cap
    rows.append(("MCC", 2, "2026-02-01 00:00:00", 1, 0.0, 2.0))
    rows.append(("MCC", 2, "2026-02-01 00:00:05", 2, 2.0, 0.0))     # cap breaks the run
    rows.append(("MCC", 2, "2026-02-01 00:00:10", 3, 0.0, 2.0))
    for m, h, ts, seq, tq, st in rows:
        con.execute("INSERT INTO cap_events VALUES (?,?,?,?,?,?,1,false,false,false)",
                    [m, h, ts, seq, tq, st])
    con.close()
    return str(path)


def test_finds_the_sustained_run_and_ignores_isolated_cycles(idle_store):
    cfg = Config(store_path=idle_store, idle_min_seconds=30)
    r = idle_periods(cfg, period="2026-02")
    assert r.status == "ok"
    periods = r.values["periods"]
    assert len(periods) == 1                     # head 2's isolated cycles don't qualify
    p = periods[0]
    assert p["head_id"] == 1
    assert p["duration_seconds"] == 60
    assert p["cycles"] == 7


def test_threshold_above_the_run_finds_nothing(idle_store):
    cfg = Config(store_path=idle_store, idle_min_seconds=300)
    r = idle_periods(cfg, period="2026-02")
    assert r.status == "insufficient_data"
    assert "no idle periods" in r.message


def test_min_seconds_argument_overrides_config(idle_store):
    cfg = Config(store_path=idle_store, idle_min_seconds=300)
    r = idle_periods(cfg, period="2026-02", min_seconds=30)
    assert r.status == "ok"
    assert len(r.values["periods"]) == 1


@pytest.fixture
def sustained_run_store(tmp_path):
    """One sustained no-load run on head 1, ended by a real cap.

    This fixture used to place two no-load cycles at the SAME timestamp with
    distinct cap_seq, which was legal while the store was keyed on
    (machine_id, head_id, cap_seq). It no longer is: the identity is
    (machine_id, head_id, ts), because a head closes at most once per poll and
    caps missed between polls arrive as one event with delta > 1. Duplicate
    per-head timestamps are now unrepresentable, so the fragmentation the old
    test guarded against cannot reach the query. The invariant that makes it
    unreachable is asserted separately below.
    """
    path = tmp_path / "dup.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE cap_events (
            machine_id VARCHAR NOT NULL, head_id SMALLINT NOT NULL, ts TIMESTAMP,
            cap_seq BIGINT NOT NULL, app_torque REAL, status REAL, delta INTEGER,
            is_fault BOOLEAN, aggregated BOOLEAN, is_reset BOOLEAN,
            UNIQUE (machine_id, head_id, ts))
    """)
    rows = [
        ("MCC", 1, "2026-02-01 00:00:00", 1, 0.0, 2.0),
        ("MCC", 1, "2026-02-01 00:00:10", 2, 0.0, 2.0),
        ("MCC", 1, "2026-02-01 00:00:20", 3, 0.0, 2.0),
        ("MCC", 1, "2026-02-01 00:00:40", 4, 0.0, 2.0),
        ("MCC", 1, "2026-02-01 00:01:00", 5, 0.0, 2.0),   # run spans 00:00:00 -> 00:01:00 = 60s
        ("MCC", 1, "2026-02-01 00:02:00", 6, 2.0, 0.0),   # real cap ends the run
    ]
    for m, h, ts, seq, tq, st in rows:
        con.execute("INSERT INTO cap_events VALUES (?,?,?,?,?,?,1,false,false,false)",
                    [m, h, ts, seq, tq, st])
    con.close()
    return str(path)


def test_sustained_run_is_one_island(sustained_run_store):
    cfg = Config(store_path=sustained_run_store, idle_min_seconds=30)
    r = idle_periods(cfg, period="2026-02")
    assert r.status == "ok"
    periods = r.values["periods"]
    assert len(periods) == 1
    assert periods[0]["cycles"] == 5
    assert periods[0]["duration_seconds"] == 60
    assert r.values["total_idle_seconds"] == 60


def test_store_rejects_two_events_for_one_head_at_one_timestamp(tmp_path):
    """The invariant the gaps-and-islands query now relies on.

    A head cannot close twice within one poll, so two rows sharing (head_id, ts)
    are the same observation recorded twice. Enforcing that in the schema is what
    lets the island id stay stable without a cap_seq tiebreaker.
    """
    path = tmp_path / "dup_reject.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE cap_events (
            machine_id VARCHAR NOT NULL, head_id SMALLINT NOT NULL, ts TIMESTAMP,
            cap_seq BIGINT NOT NULL, app_torque REAL, status REAL, delta INTEGER,
            is_fault BOOLEAN, aggregated BOOLEAN, is_reset BOOLEAN,
            UNIQUE (machine_id, head_id, ts))
    """)
    con.execute("INSERT INTO cap_events VALUES "
                "('MCC',1,'2026-02-01 00:00:00',1,0.0,2.0,1,false,false,false)")
    with pytest.raises(duckdb.ConstraintException):
        con.execute("INSERT INTO cap_events VALUES "
                    "('MCC',1,'2026-02-01 00:00:00',2,0.0,2.0,1,false,false,false)")
    con.close()


def test_a_hole_in_the_data_breaks_the_run_instead_of_being_absorbed(tmp_path):
    """A window with no rows at all is downtime, not no-load cycling.

    The islands were built from consecutive *no-load rows*, with nothing
    bounding the gap between them, and duration is MAX(ts) - MIN(ts) over the
    island. So a machine switched off between two no-load cycles had its whole
    off-window counted as idling: three rows at 00:00:00-02 on 2026-02-01 and
    three more on 2026-02-06 came back as ONE period, cycles=6,
    duration_seconds=432002 -- 120 hours of "idle" from six seconds of cycling.
    The module docstring says the opposite in as many words ("a machine that is
    switched off produces no rows and no islands"), and February's headline
    11,551.3 head-hours is 47.7% of the month, with nothing saying how much of
    it was absorbed downtime.
    """
    path = tmp_path / "gap.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE cap_events (
            machine_id VARCHAR NOT NULL, head_id SMALLINT NOT NULL, ts TIMESTAMP,
            cap_seq BIGINT NOT NULL, app_torque REAL, status REAL, delta INTEGER,
            is_fault BOOLEAN, aggregated BOOLEAN, is_reset BOOLEAN,
            UNIQUE (machine_id, head_id, ts))
    """)
    rows = []
    for i in range(4):                       # 40 s of real no-load cycling
        rows.append(("MCC", 1, f"2026-02-01 00:00:{i * 10:02d}", i + 1, 0.0, 2.0))
    for i in range(4):                       # five days later, more cycling
        rows.append(("MCC", 1, f"2026-02-06 00:00:{i * 10:02d}", 100 + i, 0.0, 2.0))
    for m, h, ts, seq, tq, st in rows:
        con.execute("INSERT INTO cap_events VALUES (?,?,?,?,?,?,1,false,false,false)",
                    [m, h, ts, seq, tq, st])
    con.close()

    cfg = Config(store_path=str(path), idle_min_seconds=30)
    r = idle_periods(cfg, period="2026-02")
    assert r.status == "ok"
    periods = r.values["periods"]
    assert len(periods) == 2, "the five-day hole must not be one idle period"
    assert [p["duration_seconds"] for p in periods] == [30, 30]
    assert sum(p["cycles"] for p in periods) == 8
    assert any("gap" in a for a in r.provenance.assumptions), \
        "the gap bound is a stated assumption, not a silent one"
