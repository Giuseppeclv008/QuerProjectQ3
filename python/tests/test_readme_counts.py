"""The README's test counts, asserted instead of maintained by hand.

They have now been wrong three times -- 63 against 73, 206 against 226, and 80
against 85 -- always the same way: someone adds tests and the paragraph that
counts them is two thousand lines away in another file. A number a reader cannot
check is worse than no number, because the reader believes it.

The C++ side is counted from the sources rather than from ctest, so this runs
without a build directory: gtest_discover_tests registers one ctest entry per
TEST/TEST_F macro, so the two agree by construction (verified: 85 and 85).

The Python side is the suite's own collection, which equals the whole suite only
when the whole suite was collected -- so a filtered run skips rather than failing
on a number it was never in a position to see.

Each count appears twice in the README, in the prose and in the command comment,
and both are checked: they have drifted apart from each other before.
"""
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _claimed(pattern):
    """The single number the README states for `pattern`, or a failure saying why not."""
    text = (_ROOT / "README.md").read_text(encoding="utf-8")
    found = {int(m.replace(",", "")) for m in re.findall(pattern, text)}
    assert found, f"README carries no count matching {pattern!r}"
    assert len(found) == 1, (
        f"README states more than one value for {pattern!r}: {sorted(found)}"
    )
    return found.pop()


def test_readme_cpp_test_count_matches_the_sources():
    actual = sum(
        len(re.findall(r"^(?:TEST|TEST_F)\(", f.read_text(encoding="utf-8"), re.M))
        for f in sorted((_ROOT / "tests").glob("*.cpp"))
    )
    assert actual > 0, "found no TEST macros; has tests/ moved?"
    assert _claimed(r"\*\*([\d,]+) C\+\+ unit tests\*\*") == actual
    assert _claimed(r"#\s*([\d,]+) C\+\+ tests") == actual


def test_readme_python_test_count_matches_collection(request):
    config = request.config
    if config.option.keyword or config.option.markexpr:
        pytest.skip("filtered run; the collected count is not the suite's")
    # Naming a path is filtering too, and it is the filter that actually bites:
    # two of the suite's files (test_bench_plots.py, test_oracle.py) sit in
    # python/ rather than python/tests/, so `pytest python/tests` collects four
    # fewer than the whole suite and this test read the shortfall as a stale
    # README. Only the run that asked for everything is in a position to count.
    targets = {Path(a.split("::")[0]).resolve() for a in config.args}
    if targets != {_ROOT / "python"}:
        pytest.skip("path-scoped run; the collected count is not the suite's")
    collected = len(request.session.items)
    assert _claimed(r"\*\*([\d,]+)\s*\nPython tests\*\*") == collected
    assert _claimed(r"#\s*([\d,]+) Python tests") == collected
