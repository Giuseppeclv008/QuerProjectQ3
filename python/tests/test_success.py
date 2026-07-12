import pytest
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
