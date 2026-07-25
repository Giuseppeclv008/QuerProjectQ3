"""Plots must survive degenerate data, because reports are generated unattended.

A tool that returns insufficient_data must produce no plot and no exception --
the report then simply omits the figure and says why in confidence/limits.
"""
from analytics.report import plots
from analytics.result import ToolResult
from analytics.tools.anomaly import anomalies
from analytics.tools.speed import capping_speed
from analytics.tools.success import success_rates
from analytics.tools.trend import trend


def test_success_rate_per_head_writes_a_png(tiny_cfg, tmp_path):
    result = success_rates(tiny_cfg, period="2026-02", by="head")
    name = plots.success_rate_per_head(result, tmp_path)
    assert name and (tmp_path / name).stat().st_size > 0


def test_capping_speed_writes_a_png(tiny_cfg, tmp_path):
    result = capping_speed(tiny_cfg, period="2026-02", bucket="hour")
    name = plots.capping_speed_over_time(result, tmp_path)
    assert name and (tmp_path / name).stat().st_size > 0


def test_torque_rolling_mean_writes_a_png(tiny_cfg, tmp_path):
    result = trend(tiny_cfg, period="2026-02", signal="torque", by="hour", window=2)
    name = plots.torque_rolling_mean(result, tmp_path)
    assert name and (tmp_path / name).stat().st_size > 0


def test_drift_ranking_writes_a_png(tiny_cfg, tmp_path):
    result = trend(tiny_cfg, period="2026-02", signal="torque", by="hour", window=2)
    name = plots.drift_ranking(result, tmp_path)
    assert name and (tmp_path / name).stat().st_size > 0


def test_anomalies_over_time_writes_a_png(tiny_cfg, tmp_path):
    result = anomalies(tiny_cfg, period="2026-02", method="both")
    name = plots.anomalies_over_time(result, tmp_path)
    assert name and (tmp_path / name).stat().st_size > 0


def test_every_plot_returns_none_on_an_error_result(tmp_path):
    bad = ToolResult.error("whatever", "boom")
    for fn in (plots.success_rate_per_head, plots.capping_speed_over_time,
               plots.torque_rolling_mean, plots.drift_ranking,
               plots.anomalies_over_time):
        assert fn(bad, tmp_path) is None
    assert not list(tmp_path.iterdir())


def test_every_plot_returns_none_on_insufficient_data(tmp_path):
    empty = ToolResult.insufficient("whatever", "no rows")
    for fn in (plots.success_rate_per_head, plots.capping_speed_over_time,
               plots.torque_rolling_mean, plots.drift_ranking,
               plots.anomalies_over_time):
        assert fn(empty, tmp_path) is None


def test_anomalies_plot_returns_none_when_nothing_was_flagged(tiny_cfg, tmp_path):
    # tiny_store's torque band excludes nothing and MAD is degenerate, so a store
    # with no hits at all must not produce an empty axes.
    result = anomalies(tiny_cfg, period="2026-07", method="both")
    assert plots.anomalies_over_time(result, tmp_path) is None
