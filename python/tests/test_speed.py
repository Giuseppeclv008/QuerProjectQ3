import pytest
from analytics.tools.speed import capping_speed


def test_pieces_per_hour_counts_only_real_caps(tiny_cfg):
    r = capping_speed(tiny_cfg, period="2026-02", bucket="hour")
    assert r.status == "ok"
    buckets = r.values["buckets"]
    assert len(buckets) == 1
    # 5 closures with load in the 00:00 hour (no-load cycles do not produce pieces).
    assert buckets[0]["caps"] == 5
    assert buckets[0]["pieces_per_hour"] == pytest.approx(5.0)


def test_day_bucket_normalises_to_an_hourly_rate(tiny_cfg):
    r = capping_speed(tiny_cfg, period="2026-02", bucket="day")
    b = r.values["buckets"][0]
    assert b["caps"] == 5
    assert b["pieces_per_hour"] == pytest.approx(5 / 24)


def test_empty_period_is_insufficient(tiny_cfg):
    r = capping_speed(tiny_cfg, period="2026-09")
    assert r.status == "insufficient_data"


def test_rejects_unknown_bucket(tiny_cfg):
    r = capping_speed(tiny_cfg, period="2026-02", bucket="fortnight")
    assert r.status == "error"
