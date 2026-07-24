import duckdb
import pytest
from analytics.config import Config
from analytics.tools.success import success_rates


def test_success_rate_per_head_excludes_no_load(tiny_cfg):
    r = success_rates(tiny_cfg, period="2026-02", by="head")
    assert r.status == "ok"
    by_head = {v["head_id"]: v for v in r.values}

    # Head 1: 3 successful, 0 failed -> 100%
    assert by_head[1]["total"] == 3
    assert by_head[1]["success_rate"] == pytest.approx(1.0)

    # Head 2: 1 successful, 1 fault -> 50%
    assert by_head[2]["total"] == 2
    assert by_head[2]["failed"] == 1
    assert by_head[2]["success_rate"] == pytest.approx(0.5)

    # Head 3 did only no-load cycles: it performed ZERO capping operations, so it
    # must NOT appear with a fabricated 0% success rate.
    assert 3 not in by_head


def test_overall_identifies_the_lowest_head(tiny_cfg):
    r = success_rates(tiny_cfg, period="2026-02", by="overall")
    v = r.values
    assert v["total"] == 5
    assert v["successful"] == 4
    assert v["success_rate"] == pytest.approx(0.8)
    assert v["lowest_head"] == 2


def test_by_day(tiny_cfg):
    r = success_rates(tiny_cfg, period="2026-02", by="day")
    assert len(r.values) == 1
    day = r.values[0]
    assert str(day["day"]) == "2026-02-01"
    assert day["successful"] == 4
    # Pin the no-load exclusion on the day axis too, not just the head axis:
    # 5 capping operations, not the 7 closures in the store.
    assert day["total"] == 5
    assert day["success_rate"] == pytest.approx(0.8)


def test_empty_period_is_insufficient(tiny_cfg):
    r = success_rates(tiny_cfg, period="2026-09", by="head")
    assert r.status == "insufficient_data"


@pytest.mark.parametrize("by", ["head", "day", "overall"])
def test_empty_period_never_raises_on_any_grouping(tiny_cfg, by):
    # The overall branch divides by its own total. If its zero-guard is ever
    # weakened, that becomes a ZeroDivisionError on exactly the empty-period case
    # the "tool errors are values, never exceptions" contract exists to prevent —
    # and this is the flagship KPI an unattended agent calls.
    r = success_rates(tiny_cfg, period="2026-09", by=by)
    assert r.status == "insufficient_data"
    assert r.values == {}


def test_rejects_unknown_grouping(tiny_cfg):
    r = success_rates(tiny_cfg, period="2026-02", by="wombat")
    assert r.status == "error"
    assert "by must be" in r.message


@pytest.fixture
def odd_status_store(tmp_path):
    """Real data carries capping operations whose status is neither success (0) nor
    fault (65): status 2 with torque, status 9, status 4. They are capping
    operations (torque > 0) but not a pass/fail verdict.
       Head 1: 3 successes + 1 fault + 2 odd-status (status 9) caps.
       Head 5: only odd-status caps -> no verdict at all."""
    path = tmp_path / "odd.duckdb"
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
        ("MCC", 5, "2026-02-01 00:00:00", 1, 2.0, 9.0),    # head 5: only odd status
        ("MCC", 5, "2026-02-01 00:00:01", 2, 2.0, 9.0),
    ]
    for m, h, ts, seq, tq, st in rows:
        con.execute("INSERT INTO cap_events VALUES (?,?,?,?,?,?,1,false,false,false)",
                    [m, h, ts, seq, tq, st])
    con.close()
    return str(path)


def test_odd_status_caps_are_excluded_from_the_denominator(odd_status_store):
    cfg = Config(store_path=odd_status_store, machine_id="MCC")
    by_head = {v["head_id"]: v for v in success_rates(cfg, period="2026-02", by="head").values}
    # Head 1 has 6 capping operations, but only 3 successes + 1 fault are verdicts:
    # the rate is 3/(3+1) = 0.75 per spec §3.2, NOT 3/6 = 0.5.
    assert by_head[1]["total"] == 6
    assert by_head[1]["successful"] == 3
    assert by_head[1]["failed"] == 1
    assert by_head[1]["success_rate"] == pytest.approx(0.75)
    # Head 5 has capping operations but no pass/fail verdict at all: undefined (None),
    # never a 0/0 ZeroDivisionError.
    assert by_head[5]["total"] == 2
    assert by_head[5]["success_rate"] is None


def test_overall_rate_and_lowest_head_skip_verdictless_caps(odd_status_store):
    cfg = Config(store_path=odd_status_store, machine_id="MCC")
    v = success_rates(cfg, period="2026-02", by="overall").values
    # overall: 3 successes, 1 fault, 4 verdictless caps -> 3/(3+1) = 0.75.
    assert v["successful"] == 3
    assert v["failed"] == 1
    assert v["success_rate"] == pytest.approx(0.75)
    # lowest_head must skip head 5 (no verdict) and name head 1.
    assert v["lowest_head"] == 1
