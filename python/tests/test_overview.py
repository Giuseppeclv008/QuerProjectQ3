from analytics.tools.overview import overview


def test_counts_separate_real_caps_from_no_load(tiny_cfg):
    r = overview(tiny_cfg, period="2026-02")
    assert r.status == "ok"
    v = r.values
    # Heads 1 and 2 produced 5 closures with torque; head 3's 2 are no-load.
    assert v["capping_operations"] == 5      # NOT 7 — no-load is not a capping op
    assert v["successful"] == 4
    assert v["failed"] == 1
    assert v["no_load_cycles"] == 2
    assert v["heads"] == [1, 2, 3]


def test_reports_time_range(tiny_cfg):
    r = overview(tiny_cfg, period="2026-02")
    assert str(r.values["ts_min"]) == "2026-02-01 00:00:00"
    assert str(r.values["ts_max"]) == "2026-02-01 00:00:30"


def test_empty_period_is_insufficient_not_a_crash(tiny_cfg):
    r = overview(tiny_cfg, period="2026-09")
    assert r.status == "insufficient_data"
    assert "no capping events" in r.message
    assert r.values == {}


def test_provenance_records_rows_and_assumptions(tiny_cfg):
    r = overview(tiny_cfg, period="2026-02")
    assert r.provenance.rows_scanned == 7          # all closures, incl. no-load
    assert r.provenance.period == "2026-02"
    assert any("no-load" in a for a in r.provenance.assumptions)
