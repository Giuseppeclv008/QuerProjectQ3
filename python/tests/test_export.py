"""The HTML export must be a single file, or it is not an export.

A report emailed to a service engineer that loses its plots on the way is worse
than no export at all, so the PNGs are inlined and the test asserts there is no
external reference left.
"""
from pathlib import Path

from analytics.agent.executor import execute
from analytics.agent.router import canned_plan
from analytics.report import export, render


def _report_dir(tiny_cfg, tmp_path):
    ex = execute(tiny_cfg, canned_plan("kpi", "2026-02"))
    render.render(ex, tiny_cfg, tmp_path, render.summarise(ex),
                  generated_at="2026-07-24T12:00:00Z")
    return tmp_path


def test_html_is_written(tiny_cfg, tmp_path):
    path = export.to_html(_report_dir(tiny_cfg, tmp_path))
    assert Path(path).exists() and Path(path).name == "report.html"


def test_html_inlines_every_plot(tiny_cfg, tmp_path):
    d = _report_dir(tiny_cfg, tmp_path)
    html = Path(export.to_html(d)).read_text()
    assert "data:image/png;base64," in html
    for png in Path(d).glob("*.png"):
        assert f'src="{png.name}"' not in html, f"{png.name} left as an external ref"


def test_html_keeps_the_six_mandated_headings(tiny_cfg, tmp_path):
    html = Path(export.to_html(_report_dir(tiny_cfg, tmp_path))).read_text()
    for heading in ("Goal", "Data used", "Analyses executed", "Findings",
                    "Confidence and limits", "Next checks"):
        assert f">{heading}<" in html, heading


def test_pdf_returns_none_rather_than_raising_when_weasyprint_is_absent(
        tiny_cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(export, "_weasyprint", lambda: None)
    assert export.to_pdf(_report_dir(tiny_cfg, tmp_path)) is None


def test_html_contains_table_markup_for_analyses_section(tiny_cfg, tmp_path):
    html = Path(export.to_html(_report_dir(tiny_cfg, tmp_path))).read_text(encoding="utf-8")
    assert "<table" in html, "table markup not found in HTML"
    assert "<td>" in html or "<th>" in html, "table cells not found in HTML"


def test_html_utf8_roundtrip_with_non_ascii(tiny_cfg, tmp_path):
    d = _report_dir(tiny_cfg, tmp_path)
    export.to_html(d)
    html = Path(d / "report.html").read_text(encoding="utf-8")
    # The report header and data-used section contain em dash (—) and en dash (–)
    assert "—" in html, "em dash not found in HTML (UTF-8 decode failed)"
