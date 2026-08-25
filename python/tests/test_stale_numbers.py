"""No production comment may quote a figure the store rebuild retired.

`docs/validation-log.md` records the 2026-08-11 rebuild under the
`(machine_id, head_id, ts)` key: 55,132,433 rows against 20,347,822, and every
figure measured on the old store superseded with it. That entry's close-out
states "nothing else in the tree quoted the old figure outside dated log
entries" -- and two comments did. `config.py` sized `max_anomaly_items` against
678,325 February deviation hits (162,019 on the rebuilt store) and `speed.py`
justified its denominator with 7,486 idle head-hours (7,228.1 after the
idle-gap fix). Both read as current, neither was, and nothing told the reader.

Scope is `python/analytics/` on purpose. Dated entries under `docs/` are the
record of what was measured when and must keep their figures, and a test
docstring naming the number a regression produced is that number in its right
place -- a production comment is the one context where a figure can only mean
"this is what the code measures today".

Add a row when a rebuild or a method change retires a figure. The message is
what a reader needs to repair the comment, so name the current value in it.
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

# retired figure -> what it was, and what stands in its place
_RETIRED = {
    "20,347,822": "rows under the retired cap_seq key; the rebuilt store holds 55,132,433",
    "10,450,551": "February rows on the retired store; rebuilt: 21,971,506",
    "6,672,649": "February capping operations on the retired store; rebuilt: 14,824,304",
    "678,325": "February deviation hits on the retired store; current: 162,019",
    "1,734,460": "three-month deviation hits on the retired store; no three-month "
                 "idle or deviation run is committed, so quote February or nothing",
    "7,486": "February idle head-hours on the retired store; current: 7,228.1",
    "11,551.3": "February idle head-hours before the idle-gap fix; current: 7,228.1",
}


def test_no_production_source_quotes_a_retired_figure():
    offenders = []
    for path in sorted((_ROOT / "python" / "analytics").rglob("*.py")):
        for line_no, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1):
            for figure, why in _RETIRED.items():
                # Bounded, so a figure is not matched inside a longer number.
                if re.search(rf"(?<![\d,.]){re.escape(figure)}(?![\d,.])", line):
                    offenders.append(f"{path.relative_to(_ROOT)}:{line_no} "
                                     f"quotes {figure} -- {why}")
    assert not offenders, (
        "retired figure(s) in production comments; re-measure, or state the "
        "rule without the number:\n" + "\n".join(offenders))
