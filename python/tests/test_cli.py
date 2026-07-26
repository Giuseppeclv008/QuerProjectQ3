"""End to end through the CLI, on the tiny store, with no model in the loop.

This is the reproducible demo path the brief asks for, so it is tested as a whole
rather than as parts: config in, report directory out, exit code 0.
"""
import json
from pathlib import Path

import pytest

from analytics import cli


def _cfg_file(tmp_path, store_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"store_path": store_path, "machine_id": "MCC"}))
    return str(path)


@pytest.mark.parametrize("report_type", ["kpi", "drift", "anomalies"])
def test_each_report_verb_produces_a_self_contained_directory(
        tiny_store, tmp_path, report_type):
    out = tmp_path / "out"
    code = cli.main(["report", report_type,
                     "--period", "2026-02",
                     "--config", _cfg_file(tmp_path, tiny_store),
                     "--out", str(out)])
    assert code == 0
    d = out / report_type
    assert (d / "report.md").exists()
    assert (d / "report.html").exists()
    assert (d / "trace.json").exists()


def test_the_report_names_no_model_when_none_was_used(tiny_store, tmp_path):
    out = tmp_path / "out"
    cli.main(["report", "kpi", "--period", "2026-02",
              "--config", _cfg_file(tmp_path, tiny_store), "--out", str(out)])
    text = (out / "kpi" / "report.md").read_text()
    assert "No model was used to plan this report" in text


def test_ask_works_with_no_api_key(tiny_store, tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = tmp_path / "out"
    code = cli.main(["ask", "which head has the lowest success rate?",
                     "--period", "2026-02",
                     "--config", _cfg_file(tmp_path, tiny_store),
                     "--out", str(out)])
    assert code == 0
    text = (out / "ask" / "report.md").read_text()
    assert "keyword router" in text.lower() or "no anthropic client" in text.lower()


def test_an_empty_period_produces_a_report_that_says_so(tiny_store, tmp_path):
    out = tmp_path / "out"
    code = cli.main(["report", "kpi", "--period", "2026-07",
                     "--config", _cfg_file(tmp_path, tiny_store), "--out", str(out)])
    assert code == 0
    text = (out / "kpi" / "report.md").read_text()
    assert "insufficient_data" in text


def test_a_malformed_period_exits_cleanly_not_with_a_traceback(tiny_store, tmp_path):
    out = tmp_path / "out"
    code = cli.main(["report", "kpi", "--period", "February",
                     "--config", _cfg_file(tmp_path, tiny_store), "--out", str(out)])
    assert code == 0                              # the report explains the failure
    assert "February" in (out / "kpi" / "report.md").read_text()


def test_a_bad_config_exits_2_with_a_usable_message(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"torque_min": 3.0, "torque_max": 1.0}))
    assert cli.main(["report", "kpi", "--config", str(bad),
                     "--out", str(tmp_path / "o")]) == 2
    assert "torque_min" in capsys.readouterr().err


def test_a_missing_config_exits_2(tmp_path, capsys):
    assert cli.main(["report", "kpi", "--config", str(tmp_path / "nope.json"),
                     "--out", str(tmp_path / "o")]) == 2
    assert "not found" in capsys.readouterr().err


def test_an_unknown_report_type_exits_2(tiny_store, tmp_path):
    assert cli.main(["report", "quarterly",
                     "--config", _cfg_file(tmp_path, tiny_store),
                     "--out", str(tmp_path / "o")]) == 2
