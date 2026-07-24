import duckdb
import pytest
from analytics.config import Config
from analytics.tools.correlation import head_correlation


@pytest.fixture
def corr_store(tmp_path):
    """Heads 1 and 2 move together; head 3 moves opposite to both."""
    path = tmp_path / "corr.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE cap_events (
            machine_id VARCHAR NOT NULL, head_id SMALLINT NOT NULL, ts TIMESTAMP,
            cap_seq BIGINT NOT NULL, app_torque REAL, status REAL, delta INTEGER,
            is_fault BOOLEAN, aggregated BOOLEAN, is_reset BOOLEAN,
            UNIQUE (machine_id, head_id, cap_seq))
    """)
    seq = 0
    for day in range(1, 9):
        up = 1.90 + 0.02 * day
        down = 2.10 - 0.02 * day
        for head, torque in ((1, up), (2, up), (3, down)):
            seq += 1
            con.execute(
                "INSERT INTO cap_events VALUES ('MCC',?,?,?,?,0.0,1,false,false,false)",
                [head, f"2026-02-{day:02d} 12:00:00", seq, torque],
            )
    con.close()
    return str(path)


def test_correlated_heads_score_near_one(corr_store):
    cfg = Config(store_path=corr_store)
    r = head_correlation(cfg, period="2026-02")
    assert r.status == "ok"
    assert r.values["matrix"][1][2] == pytest.approx(1.0, abs=1e-6)


def test_anticorrelated_head_scores_near_minus_one(corr_store):
    cfg = Config(store_path=corr_store)
    r = head_correlation(cfg, period="2026-02")
    assert r.values["matrix"][1][3] == pytest.approx(-1.0, abs=1e-6)


def test_identifies_the_head_that_behaves_differently(corr_store):
    cfg = Config(store_path=corr_store)
    r = head_correlation(cfg, period="2026-02")
    assert r.values["outliers"][0]["head_id"] == 3


def test_selecting_a_head_pair(corr_store):
    cfg = Config(store_path=corr_store)
    r = head_correlation(cfg, period="2026-02", heads=[1, 3])
    assert sorted(r.values["matrix"]) == [1, 3]


def test_too_few_buckets_is_insufficient_not_a_crash(tiny_cfg):
    # The tiny store has a single day: correlation over one point is undefined.
    r = head_correlation(tiny_cfg, period="2026-02", by="day")
    assert r.status == "insufficient_data"
    assert "at least 2" in r.message


@pytest.fixture
def constant_head_store(tmp_path):
    """Heads 1 and 2 vary together; head 4 is frozen at a constant torque
    (zero variance -> Pearson correlation is undefined)."""
    path = tmp_path / "const.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE cap_events (
            machine_id VARCHAR NOT NULL, head_id SMALLINT NOT NULL, ts TIMESTAMP,
            cap_seq BIGINT NOT NULL, app_torque REAL, status REAL, delta INTEGER,
            is_fault BOOLEAN, aggregated BOOLEAN, is_reset BOOLEAN,
            UNIQUE (machine_id, head_id, cap_seq))
    """)
    seq = 0
    for day in range(1, 6):
        for head, torque in ((1, 1.90 + 0.02 * day), (2, 1.90 + 0.02 * day), (4, 2.00)):
            seq += 1
            con.execute(
                "INSERT INTO cap_events VALUES ('MCC',?,?,?,?,0.0,1,false,false,false)",
                [head, f"2026-02-{day:02d} 12:00:00", seq, torque],
            )
    con.close()
    return str(path)


def test_constant_head_is_none_in_matrix_and_omitted_from_outliers(constant_head_store):
    cfg = Config(store_path=constant_head_store)
    r = head_correlation(cfg, period="2026-02")
    assert r.status == "ok"
    # Head 4 has zero variance: its correlation to any peer is undefined -> None,
    assert r.values["matrix"][1][4] is None
    # and it is omitted from the outlier ranking (correlation cannot rank it) rather
    # than crashing or getting a fabricated score. The varying heads still rank.
    ranked = [o["head_id"] for o in r.values["outliers"]]
    assert 4 not in ranked
    assert set(ranked) == {1, 2}
