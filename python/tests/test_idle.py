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
            UNIQUE (machine_id, head_id, cap_seq))
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
def duplicate_ts_store(tmp_path):
    """A single sustained no-load run on head 1 in which two cycles share the SAME
    timestamp (00:00:00, distinct cap_seq). The schema's UNIQUE is on
    (machine_id, head_id, cap_seq), not ts, so this is legal data. The gaps-and-islands
    query must order both ROW_NUMBER() windows by (ts, cap_seq) for the island id to stay
    constant across the run; ordering by ts alone lets the two windows break the tie
    inconsistently and fragment the run."""
    path = tmp_path / "dup.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE cap_events (
            machine_id VARCHAR NOT NULL, head_id SMALLINT NOT NULL, ts TIMESTAMP,
            cap_seq BIGINT NOT NULL, app_torque REAL, status REAL, delta INTEGER,
            is_fault BOOLEAN, aggregated BOOLEAN, is_reset BOOLEAN,
            UNIQUE (machine_id, head_id, cap_seq))
    """)
    rows = [
        ("MCC", 1, "2026-02-01 00:00:00", 1, 0.0, 2.0),   # two no-load cycles
        ("MCC", 1, "2026-02-01 00:00:00", 2, 0.0, 2.0),   # at the SAME timestamp
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


def test_duplicate_timestamps_do_not_fragment_a_run(duplicate_ts_store):
    cfg = Config(store_path=duplicate_ts_store, idle_min_seconds=30)
    r = idle_periods(cfg, period="2026-02")
    assert r.status == "ok"
    periods = r.values["periods"]
    # Pins correct handling of duplicate per-head timestamps (legal data: the schema's
    # UNIQUE is on cap_seq, not ts). The (ts, cap_seq) tiebreaker gives both ROW_NUMBER
    # windows one total order, so the run stays a single island. NOTE: at this small,
    # serial scale DuckDB resolves the tie consistently even WITHOUT the tiebreaker, so
    # this documents the intended invariant rather than reproducing the scale/parallelism
    # fragmentation the reviewer observed (~16/20 trials at ~20K rows). A deterministic
    # unit test cannot force that non-determinism; a flaky scale test would break the
    # green-at-every-commit rule, so it is deliberately not added.
    assert len(periods) == 1
    assert periods[0]["cycles"] == 5
    assert periods[0]["duration_seconds"] == 60
    assert r.values["total_idle_seconds"] == 60
