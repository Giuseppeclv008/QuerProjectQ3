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


def test_zero_idle_max_gap_seconds_fails_loudly():
    with pytest.raises(ConfigError, match="idle_max_gap_seconds"):
        Config(idle_max_gap_seconds=0)


def test_the_two_idle_knobs_are_independent_in_both_directions():
    """Neither ordering of the idle knobs is a config error.

    An earlier version of this validation demanded
    `idle_max_gap_seconds >= idle_min_seconds`, justified by "a hole shorter
    than the gap bound is absorbed into a run it is long enough to be an idle
    period of on its own". That is backwards: idle.py breaks a run on holes
    *longer* than the gap bound, so for an absorbed hole to qualify as an idle
    period by itself you need `min_seconds <= hole <= max_gap` -- which is
    exactly the relation the check was demanding rather than the one it
    prevented. It also rejected the conservative direction: max_gap = 60 with
    min_seconds = 300 breaks a run at any 60-second hole and is strictly harder
    to inflate than the defaults, yet it raised at startup.

    Both orderings are coherent. The gap bound constrains continuity of the
    data; the minimum constrains duration of the result.
    """
    strict = Config(idle_min_seconds=300, idle_max_gap_seconds=60)
    assert strict.idle_max_gap_seconds == 60

    permissive = Config(idle_min_seconds=300, idle_max_gap_seconds=600)
    assert permissive.idle_max_gap_seconds == 600

    equal = Config(idle_min_seconds=300, idle_max_gap_seconds=300)
    assert equal.idle_max_gap_seconds == 300


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


def test_nonpositive_mad_k_is_refused_at_startup():
    # mad_k <= 0 collapses the deviation band: ABS(x - median) > k*MAD is true
    # for every reading at k < 0 and for every non-median reading at k = 0.
    # It was the one threshold with no validation.
    from analytics.config import Config, ConfigError
    with pytest.raises(ConfigError):
        Config(mad_k=0.0)
    with pytest.raises(ConfigError):
        Config(mad_k=-3.0)


def test_wrong_typed_config_values_are_config_errors_at_startup():
    # {"torque_min": [1,2]} escaped as a raw TypeError and {"mad_k": "three"}
    # died mid-analysis inside DuckDB -- the docstring's "raised at startup,
    # never mid-analysis" contract, now enforced.
    import json
    from analytics.config import ConfigError, load_config
    import pytest as _pytest
    import tempfile, os
    def _load(d):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(d, fh)
            path = fh.name
        try:
            with _pytest.raises(ConfigError):
                load_config(path)
        finally:
            os.unlink(path)
    _load({"torque_min": [1, 2]})
    _load({"mad_k": "three"})
    _load({"idle_min_seconds": True})   # bool is not an int here
