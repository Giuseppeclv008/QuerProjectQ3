import pytest
from analytics.config import Config
from analytics.tools.torque import torque_stats


def test_stats_over_successful_closures_only(tiny_cfg):
    r = torque_stats(tiny_cfg, period="2026-02", outcome="successful")
    v = r.values
    # Successful torques: 2.00, 2.10, 1.90 (head 1) and 2.00 (head 2) = 4 values.
    # The fault (1.99) and the two no-load zeros are excluded.
    assert v["n"] == 4
    assert v["mean"] == pytest.approx(2.0)
    assert v["min"] == pytest.approx(1.90)
    assert v["max"] == pytest.approx(2.10)


def test_zero_torque_never_dilutes_the_mean(tiny_cfg):
    """The whole point of the semantics fix: 337k no-load zeros would drag the
    mean toward zero if they were treated as capping operations."""
    r = torque_stats(tiny_cfg, period="2026-02", outcome="all")
    assert r.values["n"] == 6          # 6 closures with load, NOT 8
    assert r.values["mean"] > 1.9      # nowhere near 0


def test_per_head_variability_ranking(tiny_cfg):
    r = torque_stats(tiny_cfg, period="2026-02", outcome="successful", by="head")
    # Head 1 (2.00, 2.10, 1.90) varies; head 2 (single value 2.00) does not.
    assert r.values[0]["head_id"] == 1
    assert r.values[0]["stddev"] > 0


def test_empty_period_is_insufficient(tiny_cfg):
    r = torque_stats(tiny_cfg, period="2026-09")
    assert r.status == "insufficient_data"


def test_rejects_unknown_outcome(tiny_cfg):
    r = torque_stats(tiny_cfg, period="2026-02", outcome="sideways")
    assert r.status == "error"


# --- Additional tests beyond the brief -------------------------------------
# The task's review checklist flags two failure modes that have bitten Tasks
# 4-5: (1) silent parameter mis-binding across every `outcome` value, and
# (2) untested early-return/guard branches. The five tests above never
# exercise outcome="failed", never send an invalid `by`, and never hit the
# by="head" empty-result branch. Each gap is closed below.

def test_stats_over_failed_closures(tiny_cfg):
    """outcome='failed' is never exercised above; without this test the
    `elif outcome == "failed": cond, sem = REJECT_SQL, []`
    branch runs uninspected in production."""
    r = torque_stats(tiny_cfg, period="2026-02", outcome="failed")
    # Head 2's two rejects match: status 65 (torque 1.99) and status 9 (torque 1.95).
    assert r.status == "ok"
    assert r.values["n"] == 2
    assert r.values["mean"] == pytest.approx((1.99 + 1.95) / 2)
    assert r.values["min"] == pytest.approx(1.95)
    assert r.values["max"] == pytest.approx(1.99)


def test_rejects_unknown_by(tiny_cfg):
    """Mirrors test_rejects_unknown_outcome for the *other* validated argument.
    'day' is deliberately chosen: it is a legal grouping for success_rates()
    but NOT for torque_stats() (only None or 'head' are), so this also pins
    that the two tools' contracts are not accidentally conflated."""
    r = torque_stats(tiny_cfg, period="2026-02", by="day")
    assert r.status == "error"
    assert "by must be" in r.message


def test_empty_period_is_insufficient_by_head(tiny_cfg):
    """The by=None empty-period guard is pinned above, but the by="head"
    branch has its OWN `if not rows:` guard (a separate query, a separate
    early return). An empty GROUP BY result must not raise or return a
    fabricated empty list -- it must be insufficient_data, same contract."""
    r = torque_stats(tiny_cfg, period="2026-09", by="head")
    assert r.status == "insufficient_data"
    assert r.values == {}


def test_single_observation_head_stddev_is_undefined_not_zero(tiny_cfg):
    """STDDEV_SAMP is NULL (SQL) for a single-row group: with one observation
    there is no variability to measure. The old `or 0.0` coercion fabricated an
    exact "sigma = 0.0000 Nm" for that head -- and made a genuine measured 0.0
    indistinguishable from "not measurable". Undefined must surface as None."""
    r = torque_stats(tiny_cfg, period="2026-02", outcome="successful", by="head")
    by_head = {v["head_id"]: v for v in r.values}
    assert by_head[2]["n"] == 1
    assert by_head[2]["stddev"] is None


def test_failed_stats_exclude_rejects_that_carried_no_load(reject_without_load_store):
    """outcome="failed" declares 'app_torque > 0 (no-load excluded)' in its
    provenance. Without the guard the zero-torque reject is included, the mean
    is dragged toward zero, and the declared filter is a false statement."""
    cfg = Config(store_path=reject_without_load_store, machine_id="MCC")
    r = torque_stats(cfg, by=None, outcome="failed")
    assert "app_torque > 0 (no-load excluded)" in " ".join(r.provenance.filters)
    assert r.values["n"] == 1, "a zero-torque reject was included in failed stats"
    assert r.values["mean"] == pytest.approx(1.98)
