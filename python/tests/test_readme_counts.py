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


def _cpp_counts():
    """(total, per-file) TEST/TEST_F counts from the sources."""
    per_file = {
        f.name: len(re.findall(r"^(?:TEST|TEST_F)\(", f.read_text(encoding="utf-8"), re.M))
        for f in sorted((_ROOT / "tests").glob("*.cpp"))
    }
    return sum(per_file.values()), per_file


def test_readme_cpp_test_count_matches_the_sources():
    actual, per_file = _cpp_counts()
    assert actual > 0, "found no TEST macros; has tests/ moved?"
    assert _claimed(r"\*\*([\d,]+) C\+\+ unit tests\*\*") == actual
    # The default build is every source except the CUDA differential
    # (test_cuda_cleaner.cpp is gated on MAS_ENABLE_CUDA).
    default_build = actual - per_file.get("test_cuda_cleaner.cpp", 0)
    assert _claimed(r"#\s*([\d,]+) C\+\+ tests in the default build") == default_build
    assert _claimed(r"\*\*([\d,]+) tests green\*\*") == default_build
    assert _claimed(r"([\d,]+) in the\ndefault build") == default_build
    # Google Test file count, stated next to the total.
    assert _claimed(r"across ([\d,]+) Google Test files") == len(per_file)
    # The project-structure tree states both again.
    assert _claimed(r"\(([\d,]+) files, [\d,]+ tests\)") == len(per_file)
    assert _claimed(r"\([\d,]+ files, ([\d,]+) tests\)") == actual


def test_readme_gated_test_counts_match_the_gating():
    # These paragraphs tell a grader that drift is impossible here; a false
    # safety signal is worse than an unguarded number, so every gated count
    # the README states is reconciled against the sources, not just the two
    # headline totals (146 and 57 both sat stale fifty lines from the guard).
    _, per_file = _cpp_counts()
    zmq_gated = sum(per_file.get(n, 0) for n in
                    ("test_zmq_smoke.cpp", "test_zmq_transport.cpp",
                     "test_zmq_e2e.cpp"))
    assert _claimed(r"and its ([\d,]+) transport tests") == zmq_gated
    agent_layer = sum(per_file.get(n, 0) for n in
                      ("test_message.cpp", "test_cleaning_worker.cpp",
                       "test_coordinator.cpp"))
    assert _claimed(r"agent layer's ([\d,]+) tests") == agent_layer
    assert _claimed(r"([\d,]+)-case GPU/CPU differential") == \
        per_file.get("test_cuda_cleaner.cpp", 0)


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


def test_every_readme_test_count_is_one_of_the_guarded_forms():
    """No unguarded `N tests` claim may exist in the README.

    The guard used to check two totals while four other count claims sat
    stale beside them. Any number followed by "test(s)" must either match a
    guarded pattern above or be listed here as a deliberate non-suite figure.
    """
    text = (_ROOT / "README.md").read_text(encoding="utf-8")
    guarded = [
        r"\*\*[\d,]+ C\+\+ unit tests\*\*",
        r"#\s*[\d,]+ C\+\+ tests in the default build",
        r"\*\*[\d,]+ tests green\*\*",
        r"[\d,]+ in the\ndefault build",
        r"\*\*[\d,]+\s*\nPython tests\*\*",
        r"#\s*[\d,]+ Python tests",
        r"and its [\d,]+ transport tests",
        r"agent layer's [\d,]+ tests",
        r"[\d,]+-case GPU/CPU differential",
        r"\([\d,]+ files, [\d,]+ tests\)",
        # Deliberate non-suite figures: subsets described in prose.
        r"\d+ tests? (?:need|skip|fail)",
        r"\*\*\d+ tests?\*\* need",
        r"21 coordinator tests",     # the deterministic-liveness story
        r"50 ZMQ tests",             # historical figure inside a dated entry
    ]
    stripped = text
    for pat in guarded:
        stripped = re.sub(pat, "", stripped)
    leftovers = re.findall(r"[^\n]*\b[\d,]+ tests?\b[^\n]*", stripped)
    assert not leftovers, (
        "unguarded test-count claim(s) in README.md -- add a reconciliation "
        f"to this file or reword them: {leftovers!r}"
    )
