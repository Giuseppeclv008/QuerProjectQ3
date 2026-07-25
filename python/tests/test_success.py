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

    # Head 2: 1 successful, 2 rejects (status 65, status 9) -> 1/3
    assert by_head[2]["total"] == 3
    assert by_head[2]["failed"] == 2
    assert by_head[2]["success_rate"] == pytest.approx(1 / 3)

    # Head 3 did only no-load cycles: it performed ZERO capping operations, so it
    # must NOT appear with a fabricated 0% success rate.
    assert 3 not in by_head


def test_overall_identifies_the_lowest_head(tiny_cfg):
    r = success_rates(tiny_cfg, period="2026-02", by="overall")
    v = r.values
    assert v["total"] == 6
    assert v["successful"] == 4
    assert v["success_rate"] == pytest.approx(4 / 6)
    assert v["lowest_head"] == 2


def test_by_day(tiny_cfg):
    r = success_rates(tiny_cfg, period="2026-02", by="day")
    assert len(r.values) == 1
    day = r.values[0]
    assert str(day["day"]) == "2026-02-01"
    assert day["successful"] == 4
    # Pin the no-load exclusion on the day axis too, not just the head axis:
    # 6 capping operations, not the 8 closures in the store.
    assert day["total"] == 6
    assert day["success_rate"] == pytest.approx(4 / 6)


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
    """Status 9 (No InTorque) has the reject bit set, same as status 65 (Bad
    Closure) -- per the brief's slide-6 bitmask both are verdicts, not the
    "unknown" status spec 12 (OQ4) once called them.
       Head 1: 3 successes + 1 status-65 reject + 2 status-9 rejects.
       Head 5: only status-9 rejects -> a verdict on every cap, and every
       verdict a reject."""
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


def test_reject_bit_status_counts_toward_the_denominator(odd_status_store):
    cfg = Config(store_path=odd_status_store, machine_id="MCC")
    by_head = {v["head_id"]: v for v in success_rates(cfg, period="2026-02", by="head").values}
    # Head 1 has 6 capping operations: 3 successes, 1 status-65 reject, 2 status-9
    # rejects. All 3 rejects count (brief slide 6): the rate is 3/(3+3) = 0.5, NOT
    # the 3/(3+1) = 0.75 that treating status 9 as unknown would give.
    assert by_head[1]["total"] == 6
    assert by_head[1]["successful"] == 3
    assert by_head[1]["failed"] == 3
    assert by_head[1]["success_rate"] == pytest.approx(0.5)
    # Head 5's caps are all status 9: every one is a reject, so unlike a true
    # verdictless head (e.g. all status 4) it gets a real 0.0 rate, not None.
    assert by_head[5]["total"] == 2
    assert by_head[5]["failed"] == 2
    assert by_head[5]["success_rate"] == pytest.approx(0.0)


def test_overall_rate_counts_reject_bit_caps_from_every_head(odd_status_store):
    cfg = Config(store_path=odd_status_store, machine_id="MCC")
    v = success_rates(cfg, period="2026-02", by="overall").values
    # overall: head 1 (3 successes, 3 rejects) + head 5 (0 successes, 2 rejects) ->
    # 3 successes, 5 rejects -> 3/(3+5) = 3/8.
    assert v["successful"] == 3
    assert v["failed"] == 5
    assert v["success_rate"] == pytest.approx(3 / 8)
    # head 5 now has a verdict on every cap (all status-9 rejects), and its 0.0
    # rate is worse than head 1's 0.5, so head 5 -- not head 1 -- is lowest.
    assert v["lowest_head"] == 5
