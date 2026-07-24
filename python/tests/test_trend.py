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


def test_rejects_nonpositive_window(tiny_cfg):
    # window <= 0 must be a value, not a raw pandas ValueError (errors-as-values).
    r = trend(tiny_cfg, period="2026-02", window=0)
    assert r.status == "error"
    assert "window" in r.message


def test_success_rate_signal_computes_per_head_rate(tiny_cfg):
    r = trend(tiny_cfg, period="2026-02", signal="success_rate", by="day")
    assert r.status == "ok"
    by_head = {s["head_id"]: s["value"] for s in r.values["series"]}
    assert by_head[1] == pytest.approx(1.0)   # head 1: 3/3 successful caps
    assert by_head[2] == pytest.approx(0.5)   # head 2: 1 successful + 1 fault


def test_rows_scanned_counts_caps_not_series_points(tiny_cfg):
    r = trend(tiny_cfg, period="2026-02", by="day")
    # 3 caps on head 1 + 2 on head 2 fall in one day -> 2 series points, but 5 capping
    # operations were examined. rows_scanned must be the examined count, not len(series).
    assert len(r.values["series"]) == 2
    assert r.provenance.rows_scanned == 5


@pytest.fixture
def odd_status_trend_store(tmp_path):
    """One head, one day: 3 successes (status 0) + 1 fault (status 65) + 2 odd-status
    (status 9) caps. The success_rate signal must read 3/(3+1)=0.75, not 3/6=0.5 --
    odd-status caps are not a verdict (locked spec §3.2), same as success_rates()."""
    path = tmp_path / "odd_trend.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE cap_events (
            machine_id VARCHAR NOT NULL, head_id SMALLINT NOT NULL, ts TIMESTAMP,
            cap_seq BIGINT NOT NULL, app_torque REAL, status REAL, delta INTEGER,
            is_fault BOOLEAN, aggregated BOOLEAN, is_reset BOOLEAN,
            UNIQUE (machine_id, head_id, cap_seq))
    """)
    rows = [
        ("MCC", 1, "2026-02-01 00:00:00", 1, 2.0, 0.0),
        ("MCC", 1, "2026-02-01 00:00:01", 2, 2.0, 0.0),
        ("MCC", 1, "2026-02-01 00:00:02", 3, 2.0, 0.0),    # 3 successes
        ("MCC", 1, "2026-02-01 00:00:03", 4, 2.0, 65.0),   # 1 fault
        ("MCC", 1, "2026-02-01 00:00:04", 5, 2.0, 9.0),    # odd status, torque > 0
        ("MCC", 1, "2026-02-01 00:00:05", 6, 2.0, 9.0),    # odd status
    ]
    for m, h, ts, seq, tq, st in rows:
        con.execute("INSERT INTO cap_events VALUES (?,?,?,?,?,?,1,false,false,false)",
                    [m, h, ts, seq, tq, st])
    con.close()
    return str(path)


def test_success_rate_signal_excludes_odd_status_caps(odd_status_trend_store):
    cfg = Config(store_path=odd_status_trend_store, machine_id="MCC")
    r = trend(cfg, period="2026-02", signal="success_rate", by="day")
    assert r.status == "ok"
    # 6 caps in the day, but only 3 successes + 1 fault are verdicts -> 3/(3+1)=0.75,
    # NOT 3/6=0.5. tiny_store can't catch this (it has no odd-status caps).
    assert [s["value"] for s in r.values["series"]] == pytest.approx([0.75])
