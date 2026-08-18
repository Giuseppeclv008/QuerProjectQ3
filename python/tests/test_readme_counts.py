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
_DECK = sorted((_ROOT / "docs" / "presentation").glob("*.md"))


def _claimed_in(text, pattern, where):
    """The single number `text` states for `pattern`, or a failure saying why not."""
    found = {int(m.replace(",", "")) for m in re.findall(pattern, text)}
    assert found, f"{where} carries no count matching {pattern!r}"
    assert len(found) == 1, (
        f"{where} states more than one value for {pattern!r}: {sorted(found)}"
    )
    return found.pop()


def _claimed(pattern):
    return _claimed_in((_ROOT / "README.md").read_text(encoding="utf-8"),
                       pattern, "README")


def _deck():
    """Both presentation files as one text; a count may live in either."""
    assert _DECK, "docs/presentation holds no .md files; has the deck moved?"
    return "\n".join(p.read_text(encoding="utf-8") for p in _DECK)


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
    # Skips were exempted from the leftover scan rather than reconciled, and the
    # README then stated 6 where the tree had 7. Count the gates instead: a
    # GTEST_SKIP in a default-build source is a test that can report green
    # without executing, which is the number a reader of "N tests green" needs.
    skippable = sum(
        len(re.findall(r"GTEST_SKIP", f.read_text(encoding="utf-8")))
        for f in sorted((_ROOT / "tests").glob("*.cpp"))
        if f.name != "test_cuda_cleaner.cpp"
    )
    assert _claimed(r"\*\*([\d,]+) C\+\+\ntests\*\* can skip") == skippable


def test_presentation_cpp_test_counts_match_the_sources():
    """The deck is guarded because it says it is.

    outline.md told its reader "both counts are asserted against the sources by
    test_readme_counts.py, so the slide cannot drift from the suite" -- and this
    file only ever read README.md. The deck was two generations behind while
    saying so (232 Python / 85 C++ against 282 / 187), which is the false safety
    signal this suite exists to prevent, relocated into the one document the
    project is defended from.
    """
    actual, per_file = _cpp_counts()
    deck = _deck()
    assert _claimed_in(deck, r"\*\*[\d,]+ Python tests, ([\d,]+) C\+\+ tests\*\*",
                       "the deck") == actual
    assert _claimed_in(deck, r"\*\*([\d,]+) C\+\+\*\* \([\d,]+ file GTest\)",
                       "the deck") == actual
    assert _claimed_in(deck, r"\(([\d,]+) file GTest\)", "the deck") == len(per_file)


def test_every_presentation_test_count_is_one_of_the_guarded_forms():
    """Same rule as the README's: no unguarded count may sit in the deck."""
    guarded = [
        r"\*\*[\d,]+ Python tests, [\d,]+ C\+\+ tests\*\*",
        r"\*\*[\d,]+ C\+\+\*\* \([\d,]+ file GTest\) \+ \*\*[\d,]+ Python\*\*",
    ]
    stripped = _deck()
    for pat in guarded:
        stripped = re.sub(pat, "", stripped)
    leftovers = re.findall(r"[^\n]*\b[\d,]+ tests?\b[^\n]*", stripped)
    assert not leftovers, (
        "unguarded test-count claim(s) in docs/presentation -- add a "
        f"reconciliation to this file or reword them: {leftovers!r}"
    )


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
    # The deck states the same total twice, in both files.
    deck = _deck()
    assert _claimed_in(deck, r"\*\*([\d,]+) Python tests, [\d,]+ C\+\+ tests\*\*",
                       "the deck") == collected
    assert _claimed_in(deck, r"\+ \*\*([\d,]+) Python\*\*", "the deck") == collected


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
        # ("21 coordinator tests" and "50 ZMQ tests" were exempted here long
        #  after the README stopped containing either. An exemption for a
        #  phrase that is not in the text guards nothing and hides that the
        #  list has drifted, so they are gone; if either claim returns it will
        #  fail here until it is reconciled, which is the point.)
    ]
    stripped = text
    for pat in guarded:
        stripped = re.sub(pat, "", stripped)
    leftovers = re.findall(r"[^\n]*\b[\d,]+ tests?\b[^\n]*", stripped)
    assert not leftovers, (
        "unguarded test-count claim(s) in README.md -- add a reconciliation "
        f"to this file or reword them: {leftovers!r}"
    )
