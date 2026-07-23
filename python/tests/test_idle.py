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
