"""The named failure paths that had no coverage at all.

Each of these is a branch the source itself documents (a comment recording a
past defect, a fallback written for a specific failure) that no test executed:
a defensive branch with no test is exactly the code most likely to be wrong
when it finally runs.
"""
import logging

import pytest

from analytics import store
from analytics.agent import narrator, planner
from analytics.agent.executor import execute
from analytics.agent.plan import Plan, PlanStep
from analytics.config import Config
from analytics.report import export
from analytics.tools.anomaly import _sample


# ------------------------------------------------------------------ store.py

def test_a_missing_store_file_raises_rather_than_creating_one(tmp_path):
    cfg = Config(store_path=str(tmp_path / "absent.duckdb"), machine_id="MCC")
    with pytest.raises(Exception):
        store.connect(cfg)
    # read_only=True must not have created the file as a side effect.
    assert not (tmp_path / "absent.duckdb").exists()


def test_a_reversed_period_range_is_refused(tiny_cfg):
    # The source comment at period_clause records this as a past defect: a
    # reversed range used to produce an empty-but-valid clause, and "no data"
    # is not the same answer as "backwards range".
    with pytest.raises(ValueError):
        store.period_clause("2026-04..2026-02")


# ------------------------------------------------------- report/export.py

def test_to_html_names_the_missing_report(tmp_path):
    # cli.py calls to_html right after render writes report.md, so this fires
    # only on a directory that is not a report directory -- the error must
    # say which file was expected rather than crash unlocated.
    with pytest.raises(FileNotFoundError) as exc:
        export.to_html(tmp_path)
    assert "report.md" in str(exc.value)


def test_to_pdf_success_path_writes_the_pdf(tmp_path, monkeypatch):
    (tmp_path / "report.md").write_text("# t\n", encoding="utf-8")

    class FakeDoc:
        def __init__(self, filename):
            self.filename = filename

        def write_pdf(self, target):
            with open(target, "wb") as fh:
                fh.write(b"%PDF-fake")

    class FakeWeasy:
        HTML = FakeDoc

    monkeypatch.setattr(export, "_weasyprint", lambda: FakeWeasy)
    path = export.to_pdf(tmp_path)
    assert path is not None and path.endswith("report.pdf")
    assert (tmp_path / "report.pdf").read_bytes().startswith(b"%PDF")


def test_to_pdf_render_failure_returns_none_not_a_crash(tmp_path, monkeypatch, caplog):
    (tmp_path / "report.md").write_text("# t\n", encoding="utf-8")

    class BrokenDoc:
        def __init__(self, filename):
            pass

        def write_pdf(self, target):
            raise RuntimeError("cairo exploded")

    class BrokenWeasy:
        HTML = BrokenDoc

    monkeypatch.setattr(export, "_weasyprint", lambda: BrokenWeasy)
    with caplog.at_level(logging.WARNING):
        assert export.to_pdf(tmp_path) is None
    assert "PDF generation failed" in caplog.text


def test_to_pdf_without_weasyprint_returns_none_with_the_remedy(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(export, "_weasyprint", lambda: None)
    with caplog.at_level(logging.WARNING):
        assert export.to_pdf(tmp_path) is None
    assert "pip install weasyprint" in caplog.text


# ------------------------------------------------------ tools/anomaly._sample

def test_sample_truncates_the_list_but_keeps_the_exact_count(tiny_cfg):
    # The entire reason _sample exists: the itemised list is capped, the
    # count is recomputed exactly with COUNT(*).
    con = store.connect(tiny_cfg)
    rows, total = _sample(
        con, "SELECT head_id, ts FROM cap_events ORDER BY ts, cap_seq", [], 3)
    assert len(rows) == 3, "the sample must be capped at the limit"
    assert total == 8, "the count must stay exact when the sample fills up"

    rows, total = _sample(
        con, "SELECT head_id, ts FROM cap_events ORDER BY ts, cap_seq", [], 100)
    assert len(rows) == 8 and total == 8, "an unfilled sample is the whole set"


# --------------------------------------------------- narrator double failure

def test_narrator_double_failure_still_returns_a_narrative(tiny_cfg, monkeypatch):
    ex = execute(tiny_cfg, Plan(goal="g", steps=[
        PlanStep("overview", {"period": "2026-02"}, "r")], source="router"))
    monkeypatch.setattr(narrator, "summarise",
                        lambda execution: (_ for _ in ()).throw(RuntimeError("boom")))
    n = narrator._fallback(ex, "model unreachable")
    assert n.source == "template"
    assert "model unreachable" in n.note
    assert "boom" in n.note, "the second failure must be disclosed too"
    assert n.findings, "narrate() promises a Narrative on every path"


# ------------------------------------------------- planner malformed payload

def test_a_payload_not_shaped_like_a_plan_falls_back(tiny_cfg, monkeypatch):
    # The model answered, but the steps are not step-shaped (no "args" key):
    # the except at planner._plan must route to the router fallback with the
    # shape error disclosed, not crash the ask.
    monkeypatch.setattr(
        planner.llm, "json_call",
        lambda cfg, client, system, prompt, schema:
            ({"goal": "g", "steps": [{"tool": "overview"}]}, None))
    plan = planner._plan(tiny_cfg, object(), "q", "2026-02")
    assert plan.source == "router"
    assert "not shaped like a plan" in plan.note
