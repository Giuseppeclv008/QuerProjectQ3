"""End to end through the CLI, on the tiny store, with no model in the loop.

This is the reproducible demo path the brief asks for, so it is tested as a whole
rather than as parts: config in, report directory out, exit code 0.
"""
import json
from pathlib import Path

import pytest

from analytics import cli
from analytics.agent import llm


@pytest.fixture(autouse=True)
def no_live_model(monkeypatch):
    """No test in this file may reach a real provider.

    Deleting ANTHROPIC_API_KEY alone was not isolation: the SDK also reads
    ANTHROPIC_AUTH_TOKEN and ANTHROPIC_BASE_URL, so on a machine carrying
    either, `ask` reached planner.plan() and narrator.narrate() with
    client=None, constructed a real client, and issued two live opus calls --
    then failed its "keyword router" assertion. Suite colour must not depend
    on the environment: force the no-client path the way test_planner and
    test_narrator already do.
    """
    # planner and narrator bind `from analytics.agent.llm import client as
    # _client` at import time, so patching llm.client alone would not reach
    # them -- patch every binding.
    from analytics.agent import narrator, planner
    monkeypatch.setattr(llm, "client", lambda cfg: None)
    monkeypatch.setattr(planner, "_client", lambda cfg: None)
    monkeypatch.setattr(narrator, "_client", lambda cfg: None)
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(var, raising=False)


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
    text = (out / "kpi" / "report.md").read_text(encoding="utf-8")
    assert "No model was used to plan this report" in text


def test_ask_works_with_no_api_key(tiny_store, tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = tmp_path / "out"
    code = cli.main(["ask", "which head has the lowest success rate?",
                     "--period", "2026-02",
                     "--config", _cfg_file(tmp_path, tiny_store),
                     "--out", str(out)])
    assert code == 0
    # Each ask lands in its own timestamped directory under out/ask, so a second
    # question cannot overwrite the first or leave the previous run's PNGs next
    # to the new report.
    runs = sorted((out / "ask").iterdir())
    assert len(runs) == 1, f"expected one run directory, got {runs}"
    text = (runs[0] / "report.md").read_text(encoding="utf-8")
    assert "keyword router" in text.lower() or "no anthropic client" in text.lower()


def test_an_empty_period_produces_a_report_that_says_so(tiny_store, tmp_path):
    out = tmp_path / "out"
    code = cli.main(["report", "kpi", "--period", "2026-07",
                     "--config", _cfg_file(tmp_path, tiny_store), "--out", str(out)])
    assert code == 0
    text = (out / "kpi" / "report.md").read_text(encoding="utf-8")
    assert "insufficient_data" in text


def test_a_malformed_period_exits_cleanly_not_with_a_traceback(tiny_store, tmp_path):
    out = tmp_path / "out"
    code = cli.main(["report", "kpi", "--period", "February",
                     "--config", _cfg_file(tmp_path, tiny_store), "--out", str(out)])
    # Cleanly = no traceback and a report that explains the failure -- but not
    # exit 0: every step errored on the malformed period, and a caller
    # (demo.sh under `set -e`) must be able to tell that from success.
    assert code == 1
    assert "February" in (out / "kpi" / "report.md").read_text(encoding="utf-8")


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


def test_a_run_where_every_step_fails_exits_nonzero(tmp_path):
    """arol used to exit 0 with every step failed (missing store -> every tool
    raises -> every ToolResult an error) -- indistinguishable from success to
    any caller, including demo.sh under `set -e`."""
    out = tmp_path / "out"
    code = cli.main(["report", "kpi", "--period", "2026-02",
                     "--config", _cfg_file(tmp_path, str(tmp_path / "nope.duckdb")),
                     "--out", str(out)])
    assert code == 1
