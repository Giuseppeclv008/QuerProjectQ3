"""Markdown -> one self-contained HTML file. PDF if the machine can do it.

Plots are inlined as base64 data URIs rather than referenced by filename: a
report that loses its figures when it is emailed or copied is not an export.

PDF is best-effort by design. WeasyPrint needs native Cairo/Pango libraries, and
making `pip install -r requirements.txt` fail on a clean machine to gain a third
output format is a bad trade. Markdown is the source of truth; HTML is the
portable artifact; PDF is a convenience when the toolchain happens to be there.
"""
import base64
import logging
import os
import re

from markdown_it import MarkdownIt

log = logging.getLogger(__name__)

_CSS = """
body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; line-height: 1.55;
       max-width: 52rem; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }
h1 { border-bottom: 2px solid #2c7fb8; padding-bottom: .3rem; }
h2 { margin-top: 2.2rem; color: #2c7fb8; }
img { max-width: 100%; border: 1px solid #ddd; border-radius: 4px; }
table { border-collapse: collapse; width: 100%; font-size: .9rem; }
th, td { border: 1px solid #ddd; padding: .4rem .6rem; text-align: left; }
th { background: #f4f6f8; }
code { background: #f4f6f8; padding: .1rem .3rem; border-radius: 3px; }
em { color: #555; }
"""


def _inline_images(html, report_dir):
    for name in sorted(os.listdir(report_dir)):
        if not name.endswith(".png"):
            continue
        with open(os.path.join(report_dir, name), "rb") as fh:
            data = base64.b64encode(fh.read()).decode("ascii")
        html = html.replace(f'src="{name}"', f'src="data:image/png;base64,{data}"')
    # Warn about any unresolved PNG references that were not inlined
    for match in re.finditer(r'src="([^"]+\.png)"', html):
        log.warning("unresolved PNG reference: %s", match.group(1))
    return html


def to_html(report_dir):
    report_dir = str(report_dir)
    with open(os.path.join(report_dir, "report.md"), encoding="utf-8") as fh:
        markdown = fh.read()
    body = MarkdownIt("commonmark", {"html": False}).enable("table").render(markdown)
    body = _inline_images(body, report_dir)
    page = (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>AROL capping report</title><style>{_CSS}</style></head>"
            f"<body>{body}</body></html>")
    path = os.path.join(report_dir, "report.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)
    log.info("wrote %s", path)
    return path


def _weasyprint():
    """Import hook, isolated so the absence path is testable."""
    try:
        import weasyprint
        return weasyprint
    except Exception:                              # noqa: BLE001 -- native deps
        return None


def to_pdf(report_dir):
    """PDF export, or None with an actionable message."""
    wp = _weasyprint()
    if wp is None:
        log.warning(
            "PDF export skipped: WeasyPrint is not installed. Markdown and HTML "
            "were written. To enable PDF: pip install weasyprint (needs Cairo and "
            "Pango; on macOS: brew install cairo pango gdk-pixbuf libffi)."
        )
        return None
    html_path = to_html(report_dir)
    pdf_path = os.path.join(str(report_dir), "report.pdf")
    try:
        wp.HTML(filename=html_path).write_pdf(pdf_path)
        log.info("wrote %s", pdf_path)
        return pdf_path
    except Exception as e:  # noqa: BLE001 -- rendering/system failures
        log.warning("PDF generation failed: %s", e)
        return None
