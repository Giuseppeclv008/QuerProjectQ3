"""Machine-checks for the README's reproducibility surface.

The hardest reproduction breaker the full-codebase review found was six
documented commands globbing raw data with a pattern that matched nothing
(`telemetry_*/2026-02-01.csv` for files that live at
`telemetry_*/​*2026-02-01.csv`): a grader's first `./build/clean` failed. A
human sweep fixes today's instance; these checks fail on the next one at zero
maintenance cost.
"""
import glob
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_README = (_ROOT / "README.md").read_text(encoding="utf-8")


def _fenced_bash_blocks(text):
    return re.findall(r"```bash\n(.*?)```", text, re.S)


def test_every_relative_markdown_link_resolves():
    # [text](target) -- keep repo-relative targets only; external URLs and
    # in-page anchors have their own checkers.
    for target in re.findall(r"\]\(([^)#][^)]*?)(?:#[^)]*)?\)", _README):
        if re.match(r"[a-z]+://", target) or target.startswith("mailto:"):
            continue
        assert (_ROOT / target).exists(), f"README links to missing {target!r}"


def test_every_telemetry_glob_in_a_bash_block_matches_the_pool():
    # Runs only where the pool is extracted; asserting on a machine without
    # the data would fail every fresh clone for the wrong reason.
    import pytest

    if not glob.glob(str(_ROOT / "telemetry_*")):
        pytest.skip("pool not extracted; glob targets cannot exist")
    missing = []
    for block in _fenced_bash_blocks(_README):
        for g in set(re.findall(r"telemetry_\*/\S*\.csv", block)):
            if not glob.glob(str(_ROOT / g)):
                missing.append(g)
    assert not missing, (
        f"README bash blocks glob nothing on this pool: {sorted(set(missing))} "
        "(day-files live INSIDE telemetry_*/ under a prefixed name: the "
        "single-day form is telemetry_*/*YYYY-MM-DD.csv)"
    )


def test_every_repo_path_named_in_a_bash_block_exists():
    """Bare repo paths a reader will type must exist.

    Narrow by construction: only ./relative or scripts/... spellings of things
    that look like files the repo ships (not outputs the commands create, not
    globs, not build artifacts).
    """
    checked, missing = 0, []
    for block in _fenced_bash_blocks(_README):
        for tok in re.findall(r"(?:^|\s)((?:scripts|python|bench|docs)/[A-Za-z0-9_./*-]+)", block):
            if "*" in tok or tok.endswith((".duckdb", ".csv", ".json", ".parquet")):
                continue  # outputs and data, not shipped files
            checked += 1
            if not (_ROOT / tok).exists():
                missing.append(tok)
    assert checked > 0, "the README no longer names any script? check the regex"
    assert not missing, f"README bash blocks name missing files: {sorted(set(missing))}"
