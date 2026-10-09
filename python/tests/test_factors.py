"""failure_correlation: how the chance of a rejection varies with the hour or the torque."""
import math

import duckdb
import pytest

from analytics.config import Config
from analytics.tools.factors import failure_correlation, upper_gamma_q
from tests.conftest import CAP_EVENTS_DDL


def _store(tmp_path, rows):
    """rows: (hour, torque, status) -> one closure each, on head 1, 1 February."""
    path = tmp_path / "factors.duckdb"
    con = duckdb.connect(str(path))
    con.execute(CAP_EVENTS_DDL)
    for seq, (hour, torque, status) in enumerate(rows, start=1):
        con.execute("INSERT INTO cap_events VALUES ('MCC',1,?,?,?,?,1,?,false,false)",
                    [f"2026-02-01 {hour:02d}:{seq // 60 % 60:02d}:{seq % 60:02d}", seq,
                     torque, status, int(status) % 2 == 1])
    con.close()
    return Config(store_path=str(path), torque_min=1.5, torque_max=2.5)


def _hours(per_hour):
    """per_hour: {hour: (closures, rejected)} -> rows at torque 2.0."""
    rows = []
    for hour, (n, k) in per_hour.items():
        rows += [(hour, 2.0, 65.0)] * k + [(hour, 2.0, 0.0)] * (n - k)
    return rows


# ---- the chi-square tail ---------------------------------------------------

@pytest.mark.parametrize("chi2, df, p", [(3.841, 1, 0.05), (5.991, 2, 0.05),
                                         (35.172, 23, 0.05), (6.635, 1, 0.01)])
def test_the_tail_matches_the_published_critical_values(chi2, df, p):
    assert upper_gamma_q(df / 2, chi2 / 2) == pytest.approx(p, abs=5e-4)


def test_the_tail_is_one_at_zero_and_vanishes_for_a_huge_statistic():
    assert upper_gamma_q(11.5, 0.0) == 1.0
    assert upper_gamma_q(11.5, 500.0) < 1e-60


# ---- hour of day -----------------------------------------------------------

def test_a_rate_that_depends_on_the_hour_is_told_apart_from_chance(tmp_path):
    cfg = _store(tmp_path, _hours({0: (200, 20), 1: (200, 0)}))     # 10% against 0%
    v = failure_correlation(cfg, period="2026-02", by="hour_of_day").values
    assert v["highest"]["hour"] == 0 and v["lowest"]["hour"] == 1
    assert v["reject_rate"] == pytest.approx(0.05)
    assert v["degrees_of_freedom"] == 1
    assert v["p_value"] < 1e-4


def test_equal_rates_in_every_hour_give_no_evidence(tmp_path):
    cfg = _store(tmp_path, _hours({0: (200, 10), 1: (200, 10), 2: (200, 10)}))
    v = failure_correlation(cfg, period="2026-02", by="hour_of_day").values
    assert v["chi_square"] == pytest.approx(0.0, abs=1e-9)
    assert v["p_value"] == pytest.approx(1.0)


def test_the_chi_square_is_the_textbook_one(tmp_path):
    # 2x2: hour 0 has 30 of 100 rejected, hour 1 has 10 of 100.
    cfg = _store(tmp_path, _hours({0: (100, 30), 1: (100, 10)}))
    v = failure_correlation(cfg, period="2026-02", by="hour_of_day").values
    # expected rejects 20 and 20, passes 80 and 80: 2*(100/20) + 2*(100/80) = 12.5
    assert v["chi_square"] == pytest.approx(12.5)
    assert v["p_value"] == pytest.approx(upper_gamma_q(0.5, 6.25))


def test_too_few_rejects_means_no_p_value_rather_than_a_wrong_one(tmp_path):
    cfg = _store(tmp_path, _hours({0: (50, 1), 1: (50, 0)}))        # expects < 5 rejects
    v = failure_correlation(cfg, period="2026-02", by="hour_of_day").values
    assert v["p_value"] is None and "too few rejects" in v["note"]


def test_no_rejects_at_all_has_nothing_to_test(tmp_path):
    cfg = _store(tmp_path, _hours({0: (200, 0), 1: (200, 0)}))
    v = failure_correlation(cfg, period="2026-02", by="hour_of_day").values
    assert v["p_value"] is None and v["rejected"] == 0


def test_only_closures_with_a_verdict_are_counted(tmp_path):
    rows = _hours({0: (100, 10)}) + [(0, 2.0, 4.0)] * 7 + [(0, 0.0, 2.0)] * 5  # no verdict, no load
    v = failure_correlation(_store(tmp_path, rows), period="2026-02", by="hour_of_day").values
    assert v["closures"] == 100 and v["hours"][0]["closures"] == 100


# ---- torque ----------------------------------------------------------------

def _torque_rows():
    return ([(0, 1.2, 65.0)] * 6 + [(0, 1.2, 0.0)] * 4        # below the band: 6 of 10 rejected
            + [(0, 2.0, 65.0)] * 4 + [(0, 2.0, 0.0)] * 996    # inside: 4 of 1,000
            + [(0, 2.7, 0.0)] * 5)                            # above: none rejected


def test_the_reject_rate_is_given_below_inside_and_above_the_band(tmp_path):
    v = failure_correlation(_store(tmp_path, _torque_rows()), period="2026-02", by="torque").values
    zones = {z["zone"]: z for z in v["zones"]}
    assert (zones["below"]["closures"], zones["below"]["rejected"]) == (10, 6)
    assert (zones["inside"]["closures"], zones["inside"]["rejected"]) == (1000, 4)
    assert (zones["above"]["closures"], zones["above"]["rejected"]) == (5, 0)
    assert zones["below"]["reject_rate"] == pytest.approx(0.6)
    assert v["band"] == [1.5, 2.5]


def test_rejects_at_low_torque_make_the_correlation_with_success_positive(tmp_path):
    v = failure_correlation(_store(tmp_path, _torque_rows()), period="2026-02", by="torque").values
    assert 0 < v["correlation"] < 1
    assert v["mean_torque_rejected"] < v["mean_torque_successful"]


def test_the_correlation_is_pearsons_r_against_a_zero_one_outcome(tmp_path):
    rows = [(0, 1.0, 65.0), (0, 2.0, 0.0), (0, 3.0, 0.0), (0, 4.0, 0.0)]
    v = failure_correlation(_store(tmp_path, rows), period="2026-02", by="torque").values
    xs, ys = [1.0, 2.0, 3.0, 4.0], [0.0, 1.0, 1.0, 1.0]
    mx, my = sum(xs) / 4, sum(ys) / 4
    r = (sum((x - mx) * (y - my) for x, y in zip(xs, ys))
         / math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)))
    assert v["correlation"] == pytest.approx(r)


def test_no_variation_in_the_outcome_leaves_the_correlation_undefined(tmp_path):
    v = failure_correlation(_store(tmp_path, [(0, 2.0, 0.0)] * 5), period="2026-02", by="torque").values
    assert v["correlation"] is None


# ---- the contract ----------------------------------------------------------

def test_an_unknown_factor_is_an_error_not_a_crash(tiny_cfg):
    assert failure_correlation(tiny_cfg, period="2026-02", by="weather").status == "error"


@pytest.mark.parametrize("by", ["hour_of_day", "torque"])
def test_a_period_without_data_is_insufficient(tiny_cfg, by):
    assert failure_correlation(tiny_cfg, period="2026-06", by=by).status == "insufficient_data"


def test_the_default_factor_is_the_hour(tiny_cfg):
    assert failure_correlation(tiny_cfg, period="2026-02").values["by"] == "hour_of_day"
