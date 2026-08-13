"""The same tools, the same data, two storage backends — they must agree.

This is the test that makes the comparison honest. None of the eight tools know
which backend they are reading, because `connect()` presents both as a table
named `cap_events`. A difference in results is therefore a defect in one
backend, not a difference in how it was queried.
"""
import pytest

from analytics.config import Config
from analytics.tools.anomaly import anomalies
from analytics.tools.idle import idle_periods
from analytics.tools.overview import overview
from analytics.tools.speed import capping_speed
from analytics.tools.success import success_rates
from analytics.tools.torque import torque_stats

TOOLS = [
    ("overview", lambda c: overview(c, period="2026-02")),
    ("success_overall", lambda c: success_rates(c, period="2026-02", by="overall")),
    ("success_head", lambda c: success_rates(c, period="2026-02", by="head")),
    ("torque", lambda c: torque_stats(c, period="2026-02")),
    ("speed", lambda c: capping_speed(c, period="2026-02", bucket="hour")),
    ("idle", lambda c: idle_periods(c, period="2026-02")),
    ("anomalies", lambda c: anomalies(c, period="2026-02")),
]


@pytest.mark.parametrize("name,call", TOOLS, ids=[t[0] for t in TOOLS])
def test_both_backends_agree(name, call, tiny_store, tiny_store_parquet):
    duck = call(Config(store_path=tiny_store, machine_id="MCC"))
    pq = call(Config(store_path=tiny_store_parquet, machine_id="MCC"))
    assert duck.status == pq.status, f"{name}: status differs"
    assert duck.values == pq.values, f"{name}: values differ"


def test_parquet_view_deduplicates_a_redispatched_file(tmp_path, tiny_store_parquet):
    """Two files with identical events must present as one set of events.

    This is what replaces the UNIQUE constraint: the write side cannot prevent
    the duplicate, so the read side removes it.
    """
    import shutil
    from analytics.store import connect
    src = sorted(p for p in __import__("pathlib").Path(tiny_store_parquet).glob("*.parquet"))
    assert src, "fixture produced no parquet files"
    shutil.copy(src[0], src[0].with_name("redispatched.parquet"))
    con = connect(Config(store_path=tiny_store_parquet, machine_id="MCC"))
    n = con.execute("SELECT COUNT(*) FROM cap_events").fetchone()[0]
    assert n == 8, f"expected the 8 fixture events once each, got {n}"


def test_empty_parquet_directory_fails_loudly(tmp_path):
    from analytics.store import connect
    empty = tmp_path / "empty_store"
    empty.mkdir()
    with pytest.raises(ValueError, match="no .parquet files"):
        connect(Config(store_path=str(empty), machine_id="MCC"))
