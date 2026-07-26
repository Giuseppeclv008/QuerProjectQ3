import json
import pytest
from analytics.config import Config, load_config, ConfigError


def test_defaults_are_the_measured_semantics():
    cfg = load_config(None)
    assert cfg.success_status == 0.0
    assert cfg.no_load_status == 2.0


def test_load_from_file_overrides_defaults(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"store_path": "/data/x.duckdb", "torque_max": 3.0}))
    cfg = load_config(str(p))
    assert cfg.store_path == "/data/x.duckdb"
    assert cfg.torque_max == 3.0
    assert cfg.torque_min == 1.5   # untouched default


def test_torque_band_that_excludes_all_data_fails_loudly(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"torque_min": 5.0, "torque_max": 1.0}))
    with pytest.raises(ConfigError, match="torque_min .* torque_max"):
        load_config(str(p))


def test_unknown_key_fails_loudly(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"torqu_max": 3.0}))     # typo
    with pytest.raises(ConfigError, match="unknown config key"):
        load_config(str(p))


def test_direct_construction_with_bad_torque_band_fails_loudly():
    with pytest.raises(ConfigError, match="torque_min .* torque_max"):
        Config(torque_min=5.0, torque_max=1.0)


def test_direct_construction_with_zero_idle_seconds_fails_loudly():
    with pytest.raises(ConfigError, match="idle_min_seconds"):
        Config(idle_min_seconds=0)


def test_load_config_missing_file_raises_config_error():
    with pytest.raises(ConfigError, match="/nonexistent/path.json"):
        load_config("/nonexistent/path.json")


def test_load_config_malformed_json_raises_config_error(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text("{not json")
    with pytest.raises(ConfigError):
        load_config(str(p))


def test_verbose_raises_our_loggers_without_raising_our_dependencies():
    """-v exists to show the tool-use flow. Raising the ROOT logger to DEBUG
    instead buries it: markdown-it alone emits a line per parse rule per line of
    the report, which is what a real `arol ask ... -v` run produced."""
    import logging

    from analytics.log import configure

    configure(verbose=True)
    try:
        assert logging.getLogger("analytics.agent.executor").getEffectiveLevel() \
            == logging.DEBUG
        assert logging.getLogger("arol").getEffectiveLevel() == logging.DEBUG
        assert logging.getLogger("markdown_it.rules_block").getEffectiveLevel() \
            == logging.WARNING
    finally:
        configure(verbose=False)


def test_without_verbose_our_loggers_are_at_info():
    import logging

    from analytics.log import configure

    configure(verbose=False)
    assert logging.getLogger("analytics").getEffectiveLevel() == logging.INFO
