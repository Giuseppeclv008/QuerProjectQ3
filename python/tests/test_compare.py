import duckdb
import pytest

from analytics.config import Config
from analytics.tools.compare import compare_periods
from analytics.tools.success import success_rates
from tests.conftest import CAP_EVENTS_DDL


@pytest.fixture
def two_month_store(tmp_path):
    """February: 4 caps on 2 days (3 successful, 1 rejected) and 1 no-load
    cycle. March: 1 successful cap on 1 day."""
    path = tmp_path / "two_months.duckdb"
    con = duckdb.connect(str(path))
    con.execute(CAP_EVENTS_DDL)
    rows = [
        (1, "2026-02-01 00:00:00", 1, 2.0, 0.0),
        (2, "2026-02-01 00:00:00", 1, 2.0, 0.0),
        (1, "2026-02-02 00:00:00", 2, 2.0, 0.0),
        (2, "2026-02-02 00:00:00", 2, 1.9, 65.0),   # rejected, with load
        (1, "2026-02-03 00:00:00", 3, 0.0, 2.0),    # no-load
        (1, "2026-03-03 00:00:00", 4, 2.0, 0.0),
    ]
    for h, ts, seq, tq, st in rows:
        con.execute("INSERT INTO cap_events VALUES ('MCC',?,?,?,?,?,1,?,false,false)",
                    [h, ts, seq, tq, st, int(st) % 2 == 1])
    con.close()
    return str(path)


def _cmp(store, period="2026-02..2026-03", by="month"):
    return compare_periods(Config(store_path=store), period=period, by=by)


def test_one_bucket_per_month_with_the_counts(two_month_store):
    r = _cmp(two_month_store)
    assert r.status == "ok"
    feb, mar = r.values["buckets"]
    assert (feb["caps"], feb["successful"], feb["rejected"]) == (4, 3, 1)
    assert feb["no_load_cycles"] == 1
    assert (feb["active_days"], feb["calendar_days"]) == (2, 28)
    assert feb["caps_per_day"] == pytest.approx(4 / 28)
    assert feb["caps_per_active_day"] == pytest.approx(2.0)
    assert feb["reject_rate"] == pytest.approx(1 / 4)
    assert feb["no_load_share"] == pytest.approx(1 / 5)
    assert (mar["caps"], mar["calendar_days"], mar["reject_rate"]) == (1, 31, 0.0)


def test_the_reject_rate_agrees_with_success_rates(two_month_store):
    cfg = Config(store_path=two_month_store)
    overall = success_rates(cfg, period="2026-02", by="overall").values
    feb = _cmp(two_month_store).values["buckets"][0]
    assert feb["reject_rate"] == pytest.approx(1 - overall["success_rate"])


def test_change_runs_from_first_to_last_bucket(two_month_store):
    change = _cmp(two_month_store).values["change_first_to_last"]
    assert change["caps_per_day"] == pytest.approx(1 / 31 - 4 / 28)
    assert change["caps_per_day_pct"] == pytest.approx(
        100 * (1 / 31 - 4 / 28) / (4 / 28))
    assert change["reject_rate"] == pytest.approx(-0.25)


def test_the_lowest_volume_bucket_is_named(two_month_store):
    low = _cmp(two_month_store).values["lowest_volume_bucket"]
    assert str(low["bucket_start"]).startswith("2026-03")


def test_a_single_bucket_has_no_change(two_month_store):
    v = _cmp(two_month_store, period="2026-02").values
    assert len(v["buckets"]) == 1
    assert v["change_first_to_last"] is None
    assert v["lowest_volume_bucket"] is None


def test_week_buckets_are_clipped_to_the_period(two_month_store):
    # 2026-02-01 is a Sunday, so the February events fall in two ISO weeks:
    # the one starting Monday 26 January (1 day inside February) and the one
    # starting 2 February.
    v = _cmp(two_month_store, period="2026-02", by="week").values
    assert [b["calendar_days"] for b in v["buckets"]] == [1, 7]


def test_an_unknown_bucket_is_an_error_not_a_crash(two_month_store):
    assert _cmp(two_month_store, by="year").status == "error"


def test_an_empty_period_is_insufficient(two_month_store):
    assert _cmp(two_month_store, period="2026-06").status == "insufficient_data"
