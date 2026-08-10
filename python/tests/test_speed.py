import duckdb
import pytest
from analytics.config import Config
from analytics.tools.speed import capping_speed


def test_pieces_per_hour_counts_only_real_caps(tiny_cfg):
    r = capping_speed(tiny_cfg, period="2026-02", bucket="hour")
    assert r.status == "ok"
    buckets = r.values["buckets"]
    assert len(buckets) == 1
    # 6 closures with load in the 00:00 hour (no-load cycles do not produce pieces).
    assert buckets[0]["caps"] == 6
    assert buckets[0]["pieces_per_hour"] == pytest.approx(6.0)


def test_day_bucket_normalises_to_an_hourly_rate(tiny_cfg):
    r = capping_speed(tiny_cfg, period="2026-02", bucket="day")
    b = r.values["buckets"][0]
    assert b["caps"] == 6
    assert b["pieces_per_hour"] == pytest.approx(6 / 24)


def test_empty_period_is_insufficient(tiny_cfg):
    r = capping_speed(tiny_cfg, period="2026-09")
    assert r.status == "insufficient_data"


def test_rejects_unknown_bucket(tiny_cfg):
    r = capping_speed(tiny_cfg, period="2026-02", bucket="fortnight")
    assert r.status == "error"


@pytest.fixture
def two_bucket_store(tmp_path):
    """2 caps in the 00:00 hour, 4 in the 02:00 hour; the 01:00 hour holds only a
    no-load cycle. Exercises the multi-bucket averaging path and makes the
    empty-bucket exclusion observable."""
    path = tmp_path / "speed.duckdb"
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
        ("MCC", 1, "2026-02-01 00:30:00", 2, 2.0, 0.0),   # 2 caps in the 00:00 hour
        ("MCC", 1, "2026-02-01 01:00:00", 3, 0.0, 2.0),   # no-load: no piece, 01:00 stays empty
        ("MCC", 1, "2026-02-01 02:00:00", 4, 2.0, 0.0),
        ("MCC", 1, "2026-02-01 02:15:00", 5, 2.0, 0.0),
        ("MCC", 1, "2026-02-01 02:30:00", 6, 2.0, 0.0),
        ("MCC", 1, "2026-02-01 02:45:00", 7, 2.0, 0.0),   # 4 caps in the 02:00 hour
    ]
    for m, h, ts, seq, tq, st in rows:
        con.execute("INSERT INTO cap_events VALUES (?,?,?,?,?,?,1,false,false,false)",
                    [m, h, ts, seq, tq, st])
    con.close()
    return str(path)


def test_mean_is_over_active_buckets_only(two_bucket_store):
    cfg = Config(store_path=two_bucket_store, machine_id="MCC")
    r = capping_speed(cfg, period="2026-02", bucket="hour")
    assert r.status == "ok"
    buckets = r.values["buckets"]
    # The 01:00 hour holds only a no-load cycle, so no bucket is emitted for it.
    assert len(buckets) == 2
    assert [b["caps"] for b in buckets] == [2, 4]
    # Mean is over the two ACTIVE buckets: (2 + 4) / 2 = 3.0 -- NOT total pieces over
    # elapsed hours (6 / 3 = 2.0). An idle hour never lowers the reported rate.
    assert r.values["mean_pieces_per_hour"] == pytest.approx(3.0)
