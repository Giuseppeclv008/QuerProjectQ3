import json
import pytest
from analytics.config import Config, load_config, ConfigError


def test_defaults_are_the_measured_semantics():
    cfg = load_config(None)
    assert cfg.success_status == 0.0
    assert cfg.no_load_status == 2.0
    assert cfg.fault_status == 65.0


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
