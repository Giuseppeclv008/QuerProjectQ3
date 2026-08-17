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
            UNIQUE (machine_id, head_id, ts))
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
    assert r.values["deviation_hits"] == []      # method="threshold" must not compute deviation


def test_deviation_flags_the_outlier_against_its_own_heads_band(anomaly_store):
    cfg = Config(store_path=anomaly_store, torque_min=0.0, torque_max=10.0, mad_k=3.0)
    r = anomalies(cfg, period="2026-02", method="deviation")
    hits = r.values["deviation_hits"]
    # 2.9 is far outside head 1's median +/- 3*MAD, even though it is inside the
    # wide-open threshold band — this is what makes the two methods complementary.
    assert [h["app_torque"] for h in hits] == pytest.approx([2.9])
    assert r.values["threshold_hits"] == []      # method="deviation" must not compute threshold


@pytest.fixture
def stuck_head_store(tmp_path):
    """Head 1 is stuck at exactly 2.00 Nm for 20 caps -- the failure the
    deviation detector exists to catch -- plus three readings that escaped the
    stick (9.0, 8.5, 0.2). MAD and IQR are both zero."""
    path = tmp_path / "stuck.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE cap_events (
            machine_id VARCHAR NOT NULL, head_id SMALLINT NOT NULL, ts TIMESTAMP,
            cap_seq BIGINT NOT NULL, app_torque REAL, status REAL, delta INTEGER,
            is_fault BOOLEAN, aggregated BOOLEAN, is_reset BOOLEAN,
            UNIQUE (machine_id, head_id, ts))
    """)
    for i in range(20):
        con.execute("INSERT INTO cap_events VALUES ('MCC',1,?,?,2.00,0.0,1,false,false,false)",
                    [f"2026-02-01 00:00:{i:02d}", i + 1])
    for j, tq in enumerate((9.0, 8.5, 0.2)):
        con.execute("INSERT INTO cap_events VALUES ('MCC',1,?,?,?,0.0,1,false,false,false)",
                    [f"2026-02-01 00:01:{j:02d}", 100 + j, tq])
    con.close()
    return str(path)


def test_a_stuck_head_is_not_silently_dropped_from_deviation(stuck_head_store):
    # MAD = 0 used to exclude the head from the deviation query entirely, and
    # the report then made the positive claim "0 beyond their head's robust
    # band" about the one head whose statistic was undefined.
    cfg = Config(store_path=stuck_head_store, torque_min=0.0, torque_max=10.0, mad_k=3.0)
    r = anomalies(cfg, period="2026-02", method="deviation")
    hits = sorted(h["app_torque"] for h in r.values["deviation_hits"])
    assert hits == pytest.approx([0.2, 8.5, 9.0])
    assert r.values["counts"]["deviation_hits"] == 3
    # The fallback is disclosed, not silent.
    assert r.values["deviation_fallbacks"] == {1: "exact"}


@pytest.fixture
def quantized_head_store(tmp_path):
    """A quantised sensor: more than half of head 1's readings are the exact
    median 2.00 (so MAD = 0), a clustered low mode around 1.91 keeps the lower
    quartile away from the median (so IQR > 0 and the half-IQR band covers the
    cluster), and one reading at 5.0 sits far outside any band."""
    path = tmp_path / "quant.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE cap_events (
            machine_id VARCHAR NOT NULL, head_id SMALLINT NOT NULL, ts TIMESTAMP,
            cap_seq BIGINT NOT NULL, app_torque REAL, status REAL, delta INTEGER,
            is_fault BOOLEAN, aggregated BOOLEAN, is_reset BOOLEAN,
            UNIQUE (machine_id, head_id, ts))
    """)
    torques = ([2.00] * 11
               + [1.900, 1.905, 1.910, 1.915, 1.920, 1.925, 1.928, 1.930]
               + [5.0])
    for i, tq in enumerate(torques):
        con.execute("INSERT INTO cap_events VALUES ('MCC',1,?,?,?,0.0,1,false,false,false)",
                    [f"2026-02-01 00:00:{i:02d}", i + 1, tq])
    con.close()
    return str(path)


def test_a_quantized_head_falls_back_to_iqr_and_flags_only_the_outlier(quantized_head_store):
    cfg = Config(store_path=quantized_head_store, torque_min=0.0, torque_max=10.0, mad_k=3.0)
    r = anomalies(cfg, period="2026-02", method="deviation")
    hits = [h["app_torque"] for h in r.values["deviation_hits"]]
    assert hits == pytest.approx([5.0])
    assert r.values["deviation_fallbacks"] == {1: "iqr"}


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


def test_rows_scanned_counts_examined_rows_not_hits(anomaly_store):
    cfg = Config(store_path=anomaly_store, torque_min=1.5, torque_max=2.5)
    r = anomalies(cfg, period="2026-02", method="both")
    # 20 tight caps + one 2.9 cap + one fault = 22 closures examined in scope, not the
    # 3 hits (1 fault + 1 threshold + 1 deviation). Provenance must let a consumer tell
    # "0 anomalies out of 22 examined" apart from "nothing was scanned".
    assert r.provenance.rows_scanned == 22


@pytest.fixture
def multi_reject_store(tmp_path):
    """Two rejects with different condition codes: status 65 (Bad Closure) and
    status 9 (No InTorque). Both have app_torque in the default band [1.5, 2.5]
    so the threshold detector does not also fire."""
    path = tmp_path / "multi_reject.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE cap_events (
            machine_id VARCHAR NOT NULL, head_id SMALLINT NOT NULL, ts TIMESTAMP,
            cap_seq BIGINT NOT NULL, app_torque REAL, status REAL, delta INTEGER,
            is_fault BOOLEAN, aggregated BOOLEAN, is_reset BOOLEAN,
            UNIQUE (machine_id, head_id, ts))
    """)
    # Head 1: status 65 (Bad Closure, reject bit set) with torque 2.0 Nm
    con.execute("INSERT INTO cap_events VALUES ('MCC',1,'2026-02-01 00:00:00',1,2.0,65.0,1,true,false,false)")
    # Head 2: status 9 (No InTorque, reject bit set) with torque 2.2 Nm
    con.execute("INSERT INTO cap_events VALUES ('MCC',2,'2026-02-01 00:00:01',1,2.2,9.0,1,true,false,false)")
    con.close()
    return str(path)


def test_anomalies_detects_multiple_reject_codes(multi_reject_store):
    """Task 1 broadened failed from status == 65 to the reject bit, so anomalies()
    now selects REJECT_SQL, not just status 65. Both status 65 (Bad Closure) and
    status 9 (No InTorque) are rejects. The reason field decodes the condition,
    so both reasons must name their distinct codes."""
    cfg = Config(store_path=multi_reject_store)
    r = anomalies(cfg, period="2026-02", method="both")
    faults = r.values["faults"]
    assert r.values["counts"]["faults"] == 2

    # Extract reason strings and verify both appear with correct conditions
    reasons = [f["reason"] for f in faults]
    assert "Bad Closure" in reasons[0] or "Bad Closure" in reasons[1]
    assert "No InTorque" in reasons[0] or "No InTorque" in reasons[1]
    # Verify they are distinct
    assert reasons[0] != reasons[1]


def test_deviation_band_is_sigma_consistent_with_floor(tmp_path):
    """The band is k * 1.4826*MAD with a floor, not raw k*MAD.

    Raw MAD is ~0.6745 sigma, so k=3 against raw MAD was ~2.02 sigma while
    every reader of "median +/- 3*MAD" understood 3 sigma -- the ~10.9%-of-
    production flag rate in the February report was this, uncalibrated. A
    reading must escape 3 * 1.4826 * MAD to flag now.
    """
    import duckdb

    from tests.conftest import CAP_EVENTS_DDL

    path = tmp_path / "sigma.duckdb"
    con = duckdb.connect(str(path))
    con.execute(CAP_EVENTS_DDL)
    # Head 1: torques 1.9/2.0/2.1 alternating -> median 2.0, MAD 0.1,
    # sigma-consistent scale 0.14826, band at k=3: +/-0.4448.
    torques = [2.0, 1.9, 2.1, 2.0, 1.9, 2.1, 2.0]
    for i, tq in enumerate(torques):
        con.execute(
            "INSERT INTO cap_events VALUES ('MCC',1,?,?,?,0.0,1,false,false,false)",
            [f"2026-02-01 00:00:{i:02d}", i + 1, tq],
        )
    # 2.35 is 0.35 from the median: outside raw 3*MAD (0.3), INSIDE the
    # sigma-consistent 3*1.4826*MAD (0.445). It must NOT flag.
    con.execute(
        "INSERT INTO cap_events VALUES ('MCC',1,'2026-02-01 00:00:07',8,2.35,0.0,1,false,false,false)")
    # 2.5 is 0.5 out: beyond 0.445, must flag.
    con.execute(
        "INSERT INTO cap_events VALUES ('MCC',1,'2026-02-01 00:00:08',9,2.5,0.0,1,false,false,false)")
    con.close()

    res = anomalies(Config(store_path=str(path), machine_id="MCC",
                           torque_min=0.5, torque_max=5.0), period="2026-02",
                    method="deviation")
    assert res.status == "ok"
    devs = [d["app_torque"] for d in res.values["deviation_hits"]]
    assert 2.5 in devs, "a 3.37-sigma-equivalent reading must flag"
    assert 2.35 not in devs, (
        "2.35 is inside 3 sigma-equivalents; flagging it means the band is "
        "still raw MAD")
    assert "mad_floor=0.01" in res.provenance.filters


def test_the_itemised_sample_spans_the_period_instead_of_its_first_hours(tmp_path):
    """`LIMIT` on a query already ordered by ts takes the EARLIEST hits.

    plots.anomalies_over_time scatters exactly that list under the title
    "Flagged closures over time", so once the cap binds the figure shows every
    marker crammed into the start of the period and nothing after -- a reader
    sees deviations that stop. On February's 162,019 deviation hits a 5,000-item
    cap covers about 0.86 of 28 days, roughly 3% of the frame.

    The counts were always exact; it is the itemised sample, and therefore the
    picture, that was skewed. A representative sample is also what makes the
    figure honest by construction rather than by caption.
    """
    path = tmp_path / "spread.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE cap_events (
            machine_id VARCHAR NOT NULL, head_id SMALLINT NOT NULL, ts TIMESTAMP,
            cap_seq BIGINT NOT NULL, app_torque REAL, status REAL, delta INTEGER,
            is_fault BOOLEAN, aggregated BOOLEAN, is_reset BOOLEAN,
            UNIQUE (machine_id, head_id, ts))
    """)
    # 28 days, 40 out-of-band closures a day: 1,120 hits against a cap of 50.
    seq = 0
    for day in range(1, 29):
        for i in range(40):
            seq += 1
            con.execute(
                "INSERT INTO cap_events VALUES (?,?,?,?,?,0.0,1,false,false,false)",
                ["MCC", 1, f"2026-02-{day:02d} {i // 2:02d}:{(i % 2) * 30:02d}:00",
                 seq, 9.5])
    con.close()

    cfg = Config(store_path=str(path), max_anomaly_items=50)
    r = anomalies(cfg, period="2026-02", method="threshold")
    assert r.status == "ok"
    hits = r.values["threshold_hits"]
    assert r.values["counts"]["threshold_hits"] == 1120, "the count stays exact"
    # "at most `limit` rows materialised" is the contract: a ceil stride can
    # land one short of the cap, which is the right way to be wrong.
    assert 40 <= len(hits) <= 50, "the cap still bounds what is itemised"

    days = {h["ts"].day for h in hits}
    assert max(days) - min(days) >= 20, (
        f"the sample covers days {min(days)}-{max(days)}; a scatter drawn from "
        "it would show the period stopping early"
    )
