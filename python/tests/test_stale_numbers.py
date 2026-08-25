"""No production comment may quote a figure the store rebuild retired.

`docs/validation-log.md` records the 2026-08-11 rebuild under the
`(machine_id, head_id, ts)` key: 55,132,433 rows against 20,347,822, and every
figure measured on the old store superseded with it. That entry's close-out
states "nothing else in the tree quoted the old figure outside dated log
entries" -- and two comments did. `config.py` sized `max_anomaly_items` against
678,325 February deviation hits (162,019 on the rebuilt store) and `speed.py`
justified its denominator with 7,486 idle head-hours (7,228.1 after the
idle-gap fix). Both read as current, neither was, and nothing told the reader.

Scope is the production comments of both tiers -- `python/analytics/`, the
C++ under `core/` and `tests/`, the benchmark harnesses under `bench/`
(which quoted the retired pair "21.9M events vs 14.4M rows" until this week),
the top-level `python/` oracles and contenders (where the last unqualified
benchmark figure lived), `scripts/` and the root `CMakeLists.txt`.
Dated entries under `docs/` are the record of
what was measured when and must keep their figures, and `python/tests/` is out
for the same reason: a test docstring naming the number a regression produced
is that number in its right place (`test_idle.py` quotes the pre-idle-gap
11,551.3 for exactly that). The C++ tests are in scope because none of them
narrates a store figure, so the sweep is a tripwire there rather than a
licence.

Add a row when a rebuild or a method change retires a figure. The message is
what a reader needs to repair the comment, so name the current value in it.
Keep an eye on short entries: `383` is four characters of arithmetic away from
any unrelated constant, and the boundaries are all that keep it honest.
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

# Files whose comments can only mean "what the code measures today".
# (rel, globs, recurse) -- `python` is scanned non-recursively on purpose:
# python/analytics has its own entry, and python/tests stays out by the policy
# above; the top-level oracles and contenders are production comments.
_SCOPES = (
    ("python/analytics", ("*.py",), True),
    ("core", ("*.cpp", "*.hpp", "*.cu"), True),
    ("tests", ("*.cpp", "*.hpp", "*.cu"), True),
    ("bench", ("*.py", "*.sh", "*.cpp"), True),
    ("python", ("*.py",), False),
    ("scripts", ("*.sh",), True),
    (".", ("CMakeLists.txt",), False),
)

# retired figure -> what it was, and what stands in its place
_RETIRED = {
    "20,347,822": "rows under the retired cap_seq key; the rebuilt store holds 55,132,433",
    "383": "February rejected closures on the retired store; rebuilt: 748",
    "10,450,551": "February rows on the retired store; rebuilt: 21,971,506",
    "6,672,649": "February capping operations on the retired store; rebuilt: 14,824,304",
    "678,325": "February deviation hits on the retired store; current: 162,019",
    "1,734,460": "three-month deviation hits on the retired store; no three-month "
                 "idle or deviation run is committed, so quote February or nothing",
    "7,486": "February idle head-hours on the retired store; current: 7,228.1",
    "11,551.3": "February idle head-hours before the idle-gap fix; current: 7,228.1",
    "14,372,237": "February rows under the retired cap_seq key; the same 28 "
                  "day-files now persist 21,872,663",
    "1,612,634": "February deviation hits from the uncalibrated raw-MAD band; "
                 "current: 162,019",
    "22,459": "February idle periods before the idle-gap fix; current: 25,046",
}
# Not in the table, deliberately: 371/585/600/75 (the retired status==65 and
# per-head reject counts) are three digits with no separator -- 600 is the live
# idle_max_gap_seconds default -- so a sweep for them would flag ordinary
# constants. The boundaries are all that keep the short entries honest.


def _scanned():
    """Every file in scope, so a moved directory fails loudly instead of passing."""
    for rel, globs, recurse in _SCOPES:
        root = _ROOT / rel
        assert root.is_dir(), f"{rel} is not a directory; has it moved?"
        find = root.rglob if recurse else root.glob
        found = [p for g in globs for p in find(g)]
        assert found, f"{rel} holds no {'/'.join(globs)}; has it moved?"
        yield from sorted(found)


def test_no_production_source_quotes_a_retired_figure():
    offenders = []
    for path in _scanned():
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
