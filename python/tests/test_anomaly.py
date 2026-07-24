import duckdb
import pytest
from analytics.config import Config
from analytics.tools.anomaly import anomalies


@pytest.fixture
def anomaly_store(tmp_path):
    """Head 1: 20 tight caps around 2.0, plus one at 2.9 (way outside the band).
       Head 2: one fault (status 65)."""
    path = tmp_path / "anom.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE cap_events (
            machine_id VARCHAR NOT NULL, head_id SMALLINT NOT NULL, ts TIMESTAMP,
            cap_seq BIGINT NOT NULL, app_torque REAL, status REAL, delta INTEGER,
            is_fault BOOLEAN, aggregated BOOLEAN, is_reset BOOLEAN,
            UNIQUE (machine_id, head_id, cap_seq))
    """)
    for i in range(20):
        tq = 2.00 + (0.01 if i % 2 else -0.01)
        con.execute("INSERT INTO cap_events VALUES ('MCC',1,?,?,?,0.0,1,false,false,false)",
                    [f"2026-02-01 00:00:{i:02d}", i + 1, tq])
    con.execute("INSERT INTO cap_events VALUES "
                "('MCC',1,'2026-02-01 00:01:00',100,2.9,0.0,1,false,false,false)")
    con.execute("INSERT INTO cap_events VALUES "
                "('MCC',2,'2026-02-01 00:02:00',1,1.99,65.0,1,true,false,false)")
    con.close()
    return str(path)


def test_threshold_flags_torque_outside_the_configured_band(anomaly_store):
    cfg = Config(store_path=anomaly_store, torque_min=1.5, torque_max=2.5)
    r = anomalies(cfg, period="2026-02", method="threshold")
    hits = r.values["threshold_hits"]
    assert len(hits) == 1
    assert hits[0]["app_torque"] == pytest.approx(2.9)
    assert "outside band" in hits[0]["reason"]


def test_deviation_flags_the_outlier_against_its_own_heads_band(anomaly_store):
    cfg = Config(store_path=anomaly_store, torque_min=0.0, torque_max=10.0, mad_k=3.0)
    r = anomalies(cfg, period="2026-02", method="deviation")
    hits = r.values["deviation_hits"]
    # 2.9 is far outside head 1's median +/- 3*MAD, even though it is inside the
    # wide-open threshold band — this is what makes the two methods complementary.
    assert [h["app_torque"] for h in hits] == pytest.approx([2.9])


def test_faults_are_always_reported(anomaly_store):
    cfg = Config(store_path=anomaly_store)
    r = anomalies(cfg, period="2026-02", method="both")
    assert len(r.values["faults"]) == 1
    assert r.values["faults"][0]["head_id"] == 2


def test_healthy_data_yields_zero_hits_not_an_error(tiny_cfg):
    r = anomalies(tiny_cfg, period="2026-02", method="both")
    assert r.status == "ok"
    assert r.values["counts"]["threshold_hits"] == 0


def test_rejects_unknown_method(tiny_cfg):
    r = anomalies(tiny_cfg, period="2026-02", method="vibes")
    assert r.status == "error"
