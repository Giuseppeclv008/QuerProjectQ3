import duckdb
import pytest

from analytics.config import Config
from analytics.tools.overview import overview


def test_counts_separate_real_caps_from_no_load(tiny_cfg):
    r = overview(tiny_cfg, period="2026-02")
    assert r.status == "ok"
    v = r.values
    # Heads 1 and 2 produced 6 closures with torque; head 3's 2 are no-load.
    assert v["capping_operations"] == 6      # NOT 8 — no-load is not a capping op
    assert v["successful"] == 4
    assert v["failed"] == 2
    assert v["no_load_cycles"] == 2
    assert v["heads"] == [1, 2, 3]


def test_reports_time_range(tiny_cfg):
    r = overview(tiny_cfg, period="2026-02")
    assert str(r.values["ts_min"]) == "2026-02-01 00:00:00"
    assert str(r.values["ts_max"]) == "2026-02-01 00:00:40"


def test_empty_period_is_insufficient_not_a_crash(tiny_cfg):
    r = overview(tiny_cfg, period="2026-09")
    assert r.status == "insufficient_data"
    assert "no capping events" in r.message
    assert r.values == {}


def test_provenance_records_rows_and_assumptions(tiny_cfg):
    r = overview(tiny_cfg, period="2026-02")
    assert r.provenance.rows_scanned == 8          # all closures, incl. no-load
    assert r.provenance.period == "2026-02"
    assert any("no-load" in a for a in r.provenance.assumptions)


# --- WP1 validation checks -------------------------------------------------
#
# null_torque / invalid_torque / counter_resets are the validation fields this
# tool exists to provide, and the shared tiny_store contains none of the rows
# that would exercise them -- so all three read 0 no matter what the SQL says.
# This fixture is deliberately dirty. It is LOCAL to this module: the shared
# tiny_store must not change, because seven later tasks pin exact counts to it.

DIRTY_ROWS = [
    # head, ts,                     cap_seq, torque, status, is_reset
    (1, "2026-02-01 00:00:00", 1, 2.00, 0.0, False),   # normal cap
    (1, "2026-02-01 00:00:10", 2, 2.05, 0.0, False),   # normal cap
    (1, "2026-02-01 00:00:20", 3, 9.50, 0.0, False),   # torque ABOVE band
    (1, "2026-02-01 00:00:30", 4, 0.50, 0.0, False),   # torque BELOW band, still > 0
    (2, "2026-02-01 00:00:40", 1, None, 0.0, False),   # NULL torque
    (2, "2026-02-01 00:00:50", 2, 0.00, 2.0, False),   # no-load: torque 0 is NOT "invalid"
    (2, "2026-02-01 00:01:00", 3, 2.00, 0.0, True),    # counter reset marker
]


@pytest.fixture
def dirty_cfg(tmp_path):
    path = tmp_path / "dirty.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE cap_events (
            machine_id VARCHAR NOT NULL, head_id SMALLINT NOT NULL, ts TIMESTAMP,
            cap_seq BIGINT NOT NULL, app_torque REAL, status REAL, delta INTEGER,
            is_fault BOOLEAN, aggregated BOOLEAN, is_reset BOOLEAN,
            UNIQUE (machine_id, head_id, cap_seq))
    """)
    for head, ts, seq, torque, status, is_reset in DIRTY_ROWS:
        con.execute(
            "INSERT INTO cap_events VALUES ('MCC',?,?,?,?,?,1,false,false,?)",
            [head, ts, seq, torque, status, is_reset],
        )
    con.close()
    return Config(store_path=str(path), machine_id="MCC")


def test_null_torque_is_counted(dirty_cfg):
    r = overview(dirty_cfg, period="2026-02")
    assert r.values["null_torque"] == 1


def test_invalid_torque_catches_both_ends_of_the_band(dirty_cfg):
    # Default band is [1.5, 2.5]. The 9.50 row is above it and the 0.50 row is
    # below it. Asserting BOTH is what pins the OR in the filter, and what would
    # catch torque_min and torque_max being swapped.
    r = overview(dirty_cfg, period="2026-02")
    assert r.values["invalid_torque"] == 2


def test_zero_torque_no_load_is_not_invalid_torque(dirty_cfg):
    # torque == 0 means "no cap was applied", not "the torque reading is bad".
    # The SQL guards this with `app_torque > 0`; without that guard the no-load
    # row would be reported as a validation failure.
    r = overview(dirty_cfg, period="2026-02")
    assert r.values["no_load_cycles"] == 1
    assert r.values["invalid_torque"] == 2      # NOT 3


def test_counter_resets_are_counted(dirty_cfg):
    r = overview(dirty_cfg, period="2026-02")
    assert r.values["counter_resets"] == 1


def test_the_band_comes_from_config_not_from_the_code(dirty_cfg):
    # A narrower band must flag strictly more rows. If the band were hard-coded,
    # this count would not move.
    wide = overview(dirty_cfg, period="2026-02").values["invalid_torque"]

    narrow_cfg = Config(store_path=dirty_cfg.store_path, machine_id="MCC",
                        torque_min=1.99, torque_max=2.01)
    narrow = overview(narrow_cfg, period="2026-02").values["invalid_torque"]

    assert wide == 2                 # 9.50 and 0.50
    assert narrow == 3               # ...plus 2.05, now outside the tighter band
    assert narrow > wide


def test_a_reject_without_load_is_not_a_capping_failure(reject_without_load_store):
    """`failed` must apply the torque guard as well as the reject bit, or it
    disagrees with success_rates on the same store and breaks the invariant
    successful + failed <= capping_operations."""
    from analytics.tools.success import success_rates

    cfg = Config(store_path=reject_without_load_store, machine_id="MCC")
    v = overview(cfg).values
    assert v["failed"] == 1, "the zero-torque reject was counted as a capping failure"
    assert v["successful"] + v["failed"] <= v["capping_operations"]
    assert v["failed"] == success_rates(cfg, by="overall").values["failed"]
