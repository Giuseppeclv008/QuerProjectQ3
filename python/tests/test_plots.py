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


# Tests A–D: pin figure content, not just that a file appeared.
# These feed hand-built ToolResults to reach data shapes tiny_store cannot produce.


def test_success_rate_zoom_when_rates_are_near_100_percent(tmp_path, monkeypatch):
    """A. The y-axis zooms to make near-identical bars distinguishable.

    With rates [0.99994, 0.99999, 1.0], the formula must zoom the y-axis
    lower bound well above 99.0, so the bars are visually different.
    Captured y-limits are stored in a list via monkeypatched _save.
    """
    from unittest.mock import patch

    captured_ylims = []
    original_save = plots._save

    def capture_and_save(fig, out_dir, name):
        if fig.axes:
            captured_ylims.append(fig.axes[0].get_ylim())
        return original_save(fig, out_dir, name)

    monkeypatch.setattr(plots, "_save", capture_and_save)

    result = ToolResult.ok(
        "success_rates",
        [
            {"head_id": 1, "total": 100000, "successful": 99994, "failed": 6, "success_rate": 0.99994},
            {"head_id": 2, "total": 100000, "successful": 99999, "failed": 1, "success_rate": 0.99999},
            {"head_id": 3, "total": 100000, "successful": 100000, "failed": 0, "success_rate": 1.0},
        ],
        period="2026-02"
    )

    name = plots.success_rate_per_head(result, tmp_path)
    assert name is not None
    assert len(captured_ylims) == 1
    ymin, ymax = captured_ylims[0]
    # The formula with low=99.994 should yield: max(0.0, 99.994 - 0.006*0.5 - 0.01) = 99.991
    assert ymin > 99.0, f"Expected zoomed lower bound > 99.0, got {ymin}"
    assert ymax == 100.0


def test_success_rate_zoom_fallback_to_unzoomed_when_lowest_rate_is_zero(tmp_path, monkeypatch):
    """A (branch 2). When the lowest rate is 0.0, the formula falls back to unzoomed (0.0, 100.0)."""
    captured_ylims = []
    original_save = plots._save

    def capture_and_save(fig, out_dir, name):
        if fig.axes:
            captured_ylims.append(fig.axes[0].get_ylim())
        return original_save(fig, out_dir, name)

    monkeypatch.setattr(plots, "_save", capture_and_save)

    result = ToolResult.ok(
        "success_rates",
        [
            {"head_id": 1, "total": 100, "successful": 0, "failed": 100, "success_rate": 0.0},
            {"head_id": 2, "total": 100, "successful": 100, "failed": 0, "success_rate": 1.0},
        ],
        period="2026-02"
    )

    name = plots.success_rate_per_head(result, tmp_path)
    assert name is not None
    assert len(captured_ylims) == 1
    ymin, ymax = captured_ylims[0]
    # With low=0.0: max(0.0, 0.0 - 50.0*0.5 - 0.01) = 0.0 (fallback)
    assert ymin == 0.0, f"Expected fallback unzoomed lower bound 0.0, got {ymin}"
    assert ymax == 100.0


def test_torque_rolling_mean_highlights_drifting_head(tmp_path, monkeypatch):
    """B. A drifting head is highlighted with a bold red line and legend entry."""
    from analytics.tools.trend import DRIFT_TAU

    captured_axes = []
    original_save = plots._save

    def capture_and_save(fig, out_dir, name):
        if fig.axes:
            captured_axes.append(fig.axes[0])
        return original_save(fig, out_dir, name)

    monkeypatch.setattr(plots, "_save", capture_and_save)

    result = ToolResult.ok(
        "trend",
        {
            "series": [
                {"head_id": 1, "bucket": "2026-02-01", "rolling_mean": 12.5, "rolling_std": 0.1},
                {"head_id": 1, "bucket": "2026-02-02", "rolling_mean": 13.0, "rolling_std": 0.1},
                {"head_id": 1, "bucket": "2026-02-03", "rolling_mean": 13.5, "rolling_std": 0.1},
                {"head_id": 2, "bucket": "2026-02-01", "rolling_mean": 14.0, "rolling_std": 0.1},
                {"head_id": 2, "bucket": "2026-02-02", "rolling_mean": 14.1, "rolling_std": 0.1},
                {"head_id": 2, "bucket": "2026-02-03", "rolling_mean": 14.2, "rolling_std": 0.1},
            ],
            "drift": [
                {"head_id": 1, "tau": 0.8, "direction": "up", "drifting": True},
                {"head_id": 2, "tau": 0.1, "direction": "stable", "drifting": False},
            ]
        },
        period="2026-02"
    )

    name = plots.torque_rolling_mean(result, tmp_path)
    assert name is not None
    assert len(captured_axes) == 1
    ax = captured_axes[0]

    # Check that a legend exists
    legend = ax.get_legend()
    assert legend is not None, "Expected a legend when drifting heads are present"

    # Check that the legend contains "drifting" and the head ID
    handles, labels = ax.get_legend_handles_labels()
    assert len(labels) > 0
    drifting_labels = [l for l in labels if "drifting" in l.lower()]
    assert len(drifting_labels) >= 1, f"Expected at least one drifting label, got {labels}"
    assert any("1" in l for l in drifting_labels), f"Expected head 1 in drifting label, got {drifting_labels}"


def test_drift_ranking_draws_threshold_lines(tmp_path, monkeypatch):
    """C. The drift threshold lines are drawn at +/- DRIFT_TAU."""
    from analytics.tools.trend import DRIFT_TAU

    captured_axes = []
    original_save = plots._save

    def capture_and_save(fig, out_dir, name):
        if fig.axes:
            captured_axes.append(fig.axes[0])
        return original_save(fig, out_dir, name)

    monkeypatch.setattr(plots, "_save", capture_and_save)

    result = ToolResult.ok(
        "trend",
        {
            "drift": [
                {"head_id": 1, "tau": 0.8, "direction": "up", "drifting": True},
                {"head_id": 2, "tau": 0.1, "direction": "stable", "drifting": False},
            ]
        },
        period="2026-02"
    )

    name = plots.drift_ranking(result, tmp_path)
    assert name is not None
    assert len(captured_axes) == 1
    ax = captured_axes[0]

    # Get all lines (including axhlines)
    lines = ax.get_lines()

    # Extract y-values from all horizontal lines
    hline_yvalues = set()
    for line in lines:
        ydata = line.get_ydata()
        # For axhlines, ydata contains the same value repeated
        if len(ydata) > 0:
            hline_yvalues.add(ydata[0])

    # Should have horizontal lines at +DRIFT_TAU and -DRIFT_TAU
    assert DRIFT_TAU in hline_yvalues or any(abs(y - DRIFT_TAU) < 1e-9 for y in hline_yvalues), \
        f"Expected threshold line at +{DRIFT_TAU}, got y-values {hline_yvalues}"
    assert -DRIFT_TAU in hline_yvalues or any(abs(y + DRIFT_TAU) < 1e-9 for y in hline_yvalues), \
        f"Expected threshold line at -{DRIFT_TAU}, got y-values {hline_yvalues}"


def test_anomalies_over_time_plots_all_three_groups(tmp_path, monkeypatch):
    """D. All three anomaly groups are plotted when all are non-empty, with correct counts."""
    captured_axes = []
    original_save = plots._save

    def capture_and_save(fig, out_dir, name):
        if fig.axes:
            captured_axes.append(fig.axes[0])
        return original_save(fig, out_dir, name)

    monkeypatch.setattr(plots, "_save", capture_and_save)

    result = ToolResult.ok(
        "anomalies",
        {
            "faults": [
                {"head_id": 1, "ts": "2026-02-01T10:00:00", "app_torque": 15.5, "reason": "rejected"},
                {"head_id": 2, "ts": "2026-02-02T11:00:00", "app_torque": 16.0, "reason": "rejected"},
            ],
            "threshold_hits": [
                {"head_id": 1, "ts": "2026-02-03T12:00:00", "app_torque": 20.0, "reason": "outside_band"},
            ],
            "deviation_hits": [
                {"head_id": 2, "ts": "2026-02-04T13:00:00", "app_torque": 17.5, "reason": "deviation"},
                {"head_id": 1, "ts": "2026-02-05T14:00:00", "app_torque": 18.0, "reason": "deviation"},
            ],
            "counts": {}
        },
        period="2026-02"
    )

    name = plots.anomalies_over_time(result, tmp_path)
    assert name is not None
    assert len(captured_axes) == 1
    ax = captured_axes[0]

    # Check legend has three entries (one per group)
    legend = ax.get_legend()
    assert legend is not None, "Expected a legend with three groups"

    handles, labels = ax.get_legend_handles_labels()
    assert len(labels) == 3, f"Expected 3 legend entries, got {len(labels)}: {labels}"

    # Each label should mention the count in parentheses
    label_str = " ".join(labels)
    assert "(2)" in label_str, f"Expected faults count (2) in labels, got {labels}"
    assert "(1)" in label_str, f"Expected threshold_hits count (1) in labels, got {labels}"
    # deviation_hits has 2 entries
    assert label_str.count("(2)") >= 1, f"Expected deviation_hits count (2) in labels, got {labels}"
