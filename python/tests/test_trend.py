import duckdb
import pytest
from analytics.config import Config
from analytics.tools.trend import trend, mann_kendall_tau


def test_mann_kendall_detects_a_monotonic_rise():
    assert mann_kendall_tau([1, 2, 3, 4, 5]) == pytest.approx(1.0)


def test_mann_kendall_detects_a_monotonic_fall():
    assert mann_kendall_tau([5, 4, 3, 2, 1]) == pytest.approx(-1.0)


def test_mann_kendall_is_zero_for_flat_noise():
    assert abs(mann_kendall_tau([2.0, 2.0, 2.0, 2.0])) < 1e-9


def test_mann_kendall_needs_at_least_two_points():
    assert mann_kendall_tau([1.0]) == 0.0


@pytest.fixture
def drift_store(tmp_path):
    """Head 1's torque walks upward day by day; head 2 stays flat."""
    path = tmp_path / "drift.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE cap_events (
            machine_id VARCHAR NOT NULL, head_id SMALLINT NOT NULL, ts TIMESTAMP,
            cap_seq BIGINT NOT NULL, app_torque REAL, status REAL, delta INTEGER,
            is_fault BOOLEAN, aggregated BOOLEAN, is_reset BOOLEAN,
            UNIQUE (machine_id, head_id, cap_seq))
    """)
    seq = 0
    for day in range(1, 11):
        for head, torque in ((1, 1.90 + 0.02 * day), (2, 2.00)):
            seq += 1
            con.execute(
                "INSERT INTO cap_events VALUES ('MCC',?,?,?,?,0.0,1,false,false,false)",
                [head, f"2026-02-{day:02d} 12:00:00", seq, torque],
            )
    con.close()
    return str(path)


def test_flags_the_drifting_head_and_not_the_flat_one(drift_store):
    cfg = Config(store_path=drift_store)
    r = trend(cfg, period="2026-02", signal="torque", by="day", window=3)
    assert r.status == "ok"
    drift = {d["head_id"]: d for d in r.values["drift"]}
    assert drift[1]["drifting"] is True
    assert drift[1]["direction"] == "rising"
    assert drift[2]["drifting"] is False


def test_series_carries_rolling_statistics(drift_store):
    cfg = Config(store_path=drift_store)
    r = trend(cfg, period="2026-02", by="day", window=3)
    head1 = [s for s in r.values["series"] if s["head_id"] == 1]
    assert len(head1) == 10
    assert head1[-1]["rolling_mean"] is not None


def test_empty_period_is_insufficient(tiny_cfg):
    r = trend(tiny_cfg, period="2026-09")
    assert r.status == "insufficient_data"


def test_rejects_unknown_signal(tiny_cfg):
    r = trend(tiny_cfg, period="2026-02", signal="vibes")
    assert r.status == "error"
