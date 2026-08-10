"""Differential test: the vectorized cleaner must agree with oracle.py exactly.

oracle.py is the correctness reference the C++ core has been locked against
since Plan 1, so agreeing with it is agreeing with everything.
"""
import glob
import os

import pytest

import clean_vectorized
import oracle

# Anchored on this file, not on the CWD: the suite is run both from the repo
# root (`pytest python/tests`) and from python/, and a relative glob would
# silently skip the real-data case in one of them.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REAL = glob.glob(
    os.path.join(ROOT, "telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02",
                 "*2026-02-01.csv")
)


def _write_csv(path, rows, eol="\n"):
    """rows: list of (ts, counts[36], torques[36], statuses[36])."""
    with open(path, "w", newline="") as f:
        f.write(",".join(clean_vectorized.EXPECTED_HEADER) + eol)
        for ts, c, t, s in rows:
            f.write(",".join([ts] + [str(x) for x in c + t + s]) + eol)


def _row(ts, count, torque=2.5, status=0.0, n=36):
    return (ts, [count] * n, [torque] * n, [status] * n)


def test_agrees_with_oracle_on_a_synthetic_file(tmp_path):
    p = str(tmp_path / "syn.csv")
    _write_csv(p, [
        _row("2026-02-01T00:00:00.000", 100),          # seed
        _row("2026-02-01T00:00:01.000", 100),          # held
        _row("2026-02-01T00:00:02.000", 101),          # +1
        _row("2026-02-01T00:00:03.000", 106),          # +5, aggregated
        _row("2026-02-01T00:00:04.000", 3),            # reset
        _row("2026-02-01T00:00:05.000", 4, 0.0, 2.0),  # no-load after reset
    ])
    assert clean_vectorized.extract(p) == oracle.extract(p)


def test_agrees_with_oracle_on_reject_statuses(tmp_path):
    p = str(tmp_path / "rej.csv")
    _write_csv(p, [
        _row("2026-02-01T00:00:00.000", 10, 2.0, 0.0),
        _row("2026-02-01T00:00:01.000", 11, 1.9, 65.0),   # Bad Closure, reject
        _row("2026-02-01T00:00:02.000", 12, 1.8, 9.0),    # No InTorque, reject
        _row("2026-02-01T00:00:03.000", 13, 0.5, 4.0),    # No Closure, not a reject
    ])
    assert clean_vectorized.extract(p) == oracle.extract(p)


def test_crlf_yields_the_same_events(tmp_path):
    rows = [_row("2026-02-01T00:00:00.000", 100), _row("2026-02-01T00:00:01.000", 101)]
    lf, crlf = str(tmp_path / "lf.csv"), str(tmp_path / "crlf.csv")
    _write_csv(lf, rows, eol="\n")
    _write_csv(crlf, rows, eol="\r\n")
    assert clean_vectorized.extract(crlf) == clean_vectorized.extract(lf)


def test_rejects_a_header_with_the_wrong_column_count(tmp_path):
    p = str(tmp_path / "bad.csv")
    with open(p, "w") as f:
        f.write("timestamp,nope,alsonope\n1,2,3\n")
    with pytest.raises(ValueError, match="has 3 columns"):
        clean_vectorized.extract(p)


def test_rejects_a_header_with_a_wrong_column_name(tmp_path):
    # The count check short-circuits the name check, so a wrong *name* needs a
    # header of the right width to reach it.
    p = str(tmp_path / "badname.csv")
    hdr = list(clean_vectorized.EXPECTED_HEADER)
    hdr[1] = "nope"
    with open(p, "w") as f:
        f.write(",".join(hdr) + "\n")
    with pytest.raises(ValueError, match="column 1"):
        clean_vectorized.extract(p)


@pytest.mark.skipif(not REAL, reason="pool not extracted")
def test_agrees_with_oracle_on_a_real_day_file():
    got = clean_vectorized.extract(REAL[0])
    want = oracle.extract(REAL[0])
    assert len(got) == len(want)
    assert got == want
