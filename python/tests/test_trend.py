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
            UNIQUE (machine_id, head_id, ts))
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
    # A verdict is a claim of significance, so it carries its evidence: a
    # 10-bucket monotonic rise is significant at any conventional level.
    assert drift[1]["p_value"] < 0.05
    assert drift[2]["drifting"] is False


def test_mann_kendall_p_is_small_for_a_long_monotonic_rise():
    from analytics.tools.trend import mann_kendall_p
    assert mann_kendall_p(list(range(10))) < 0.001


def test_mann_kendall_p_is_one_for_a_constant_series():
    from analytics.tools.trend import mann_kendall_p
    assert mann_kendall_p([2.0] * 10) == 1.0


@pytest.fixture
def two_day_store(tmp_path):
    """Two day-buckets of plain noise: 6 heads whose torque merely changes
    between the days. With n=2 buckets, tau is +/-1 whenever the value moves at
    all -- the shape that used to flag every head as drifting."""
    path = tmp_path / "twoday.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE cap_events (
            machine_id VARCHAR NOT NULL, head_id SMALLINT NOT NULL, ts TIMESTAMP,
            cap_seq BIGINT NOT NULL, app_torque REAL, status REAL, delta INTEGER,
            is_fault BOOLEAN, aggregated BOOLEAN, is_reset BOOLEAN,
            UNIQUE (machine_id, head_id, ts))
    """)
    seq = 0
    for day in (1, 2):
        for head in range(1, 7):
            seq += 1
            torque = 2.00 + 0.01 * ((head + day) % 3)
            con.execute(
                "INSERT INTO cap_events VALUES ('MCC',?,?,?,?,0.0,1,false,false,false)",
                [head, f"2026-02-{day:02d} 12:00:00", seq, torque],
            )
    con.close()
    return str(path)


def test_two_buckets_cannot_produce_a_drift_verdict(two_day_store):
    # Under the old bare-|tau| rule every one of these heads was "drifting"
    # with tau = +/-1.00 and a maintenance action recommended. Two points
    # cannot carry a trend verdict; the entry must say so rather than guess.
    cfg = Config(store_path=two_day_store)
    r = trend(cfg, period="2026-02", by="day")
    assert r.status == "ok"          # the series itself is real data
    for d in r.values["drift"]:
        assert d["drifting"] is False
        assert d["insufficient"] is True
        assert d["p_value"] is None


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
    assert by_head[1] == pytest.approx(1.0)    # head 1: 3/3 successful caps
    assert by_head[2] == pytest.approx(1 / 3)  # head 2: 1 successful + 2 rejects


def test_rows_scanned_counts_caps_not_series_points(tiny_cfg):
    r = trend(tiny_cfg, period="2026-02", by="day")
    # 3 caps on head 1 + 3 on head 2 fall in one day -> 2 series points, but 6 capping
    # operations were examined. rows_scanned must be the examined count, not len(series).
    assert len(r.values["series"]) == 2
    assert r.provenance.rows_scanned == 6


@pytest.fixture
def odd_status_trend_store(tmp_path):
    """One head, one day: 3 successes (status 0) + 1 status-65 reject + 2 status-9
    rejects. Status 9 (No InTorque) has the reject bit set (brief slide 6), so the
    success_rate signal must read 3/(3+3)=0.5, not the 3/(3+1)=0.75 that treating
    status 9 as unknown would give -- same as success_rates()."""
    path = tmp_path / "odd_trend.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE cap_events (
            machine_id VARCHAR NOT NULL, head_id SMALLINT NOT NULL, ts TIMESTAMP,
            cap_seq BIGINT NOT NULL, app_torque REAL, status REAL, delta INTEGER,
            is_fault BOOLEAN, aggregated BOOLEAN, is_reset BOOLEAN,
            UNIQUE (machine_id, head_id, ts))
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


def test_success_rate_signal_counts_every_reject_code(odd_status_trend_store):
    cfg = Config(store_path=odd_status_trend_store, machine_id="MCC")
    r = trend(cfg, period="2026-02", signal="success_rate", by="day")
    assert r.status == "ok"
    # 6 caps in the day: 3 successes + 1 status-65 reject + 2 status-9 rejects, and
    # all 3 rejects count -> 3/(3+3)=0.5, NOT 3/(3+1)=0.75. tiny_store can't catch
    # this (it only ever carries one reject code per head).
    assert [s["value"] for s in r.values["series"]] == pytest.approx([0.5])


def test_tau_is_tie_corrected_tau_b():
    """The effect size must match the tie-corrected test it gates with.

    On a tie-dominated series (success_rate is mostly exact 1.0 buckets),
    tau-a shrinks relative to what the tie-corrected p tests -- the drift
    gate was silently stricter on that signal. Tau-b divides by the untied
    pair count: a monotone movement over the non-tied pairs reads full
    strength, and equals tau-a when there are no ties.
    """
    import math

    # 6 buckets, four tied at 1.0, movement only at the end: S = counted over
    # untied pairs. tau-a divides by D=15; tau-b by sqrt(15 * (15 - 6)).
    vals = [1.0, 1.0, 1.0, 1.0, 0.9, 0.8]
    s = sum(
        (1 if vals[j] > vals[i] else -1 if vals[j] < vals[i] else 0)
        for i in range(len(vals)) for j in range(i + 1, len(vals))
    )
    expected_b = s / math.sqrt(15 * (15 - (4 * 3 / 2)))
    assert mann_kendall_tau(vals) == pytest.approx(expected_b)
    assert abs(mann_kendall_tau(vals)) > abs(s / 15), \
        "tau-b must not be shrunk by the tied pairs the way tau-a is"

    # No ties: tau-b == tau-a exactly.
    untied = [1.0, 2.0, 3.0, 2.5, 4.0]
    s2 = sum(
        (1 if untied[j] > untied[i] else -1 if untied[j] < untied[i] else 0)
        for i in range(len(untied)) for j in range(i + 1, len(untied))
    )
    assert mann_kendall_tau(untied) == pytest.approx(s2 / 10)
