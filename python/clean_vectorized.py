"""Vectorized reference cleaner: same transform as oracle.py, no Python row loop.

oracle.py is the honest naive baseline -- an interpreted loop over 86,399 rows x
36 heads. Comparing that against C++ mostly measures the interpreter. This is the
fair Python contender: pandas parses the CSV, numpy does the transform, and the
only Python-level loop is over the emitted events.

Implements spec 2026-08-10 §3: the transform is element-wise on consecutive row
pairs, so it is one numpy.diff, not a scan.
"""
import sys

import numpy as np
import pandas as pd

NUM_HEADS = 36
# Bit 0 of the AROL status bitmask, matching oracle.is_reject and
# mas::is_reject. This file was written while oracle.py still tested
# `status in {65.0}`; that rule was corrected and this one was not, so
# status 9 (No InTorque) read as clean here. The merge that brought the
# corrected oracle onto this branch is what surfaced it.

EXPECTED_HEADER = (
    ["timestamp"]
    + [f"H{i:02d} Count" for i in range(1, NUM_HEADS + 1)]
    + [f"H{i:02d} AppTorque" for i in range(1, NUM_HEADS + 1)]
    + [f"H{i:02d} Status" for i in range(1, NUM_HEADS + 1)]
)


# Per-column dtypes rather than dtype=float64 + converters={"timestamp": str}:
# specifying both for one column makes pandas warn and drop the dtype.
_DTYPES = {c: np.float64 for c in EXPECTED_HEADER}
_DTYPES["timestamp"] = str


def _check_header(path):
    with open(path, newline="") as f:
        line = f.readline().rstrip("\r\n")
    got = line.split(",")
    if len(got) != len(EXPECTED_HEADER):
        raise ValueError(
            f"{path}: header has {len(got)} columns, expected {len(EXPECTED_HEADER)}"
        )
    for i, (g, w) in enumerate(zip(got, EXPECTED_HEADER)):
        if g != w:
            raise ValueError(f"{path}: column {i} is {g!r}, expected {w!r}")


def extract(path):
    """Return the same list of 9-tuples oracle.extract returns, same order.

    Tuple shape: (head_id, ts, cap_seq, torque, status, delta, is_fault,
                  aggregated, reset)
    """
    _check_header(path)
    # float_precision="round_trip" is not optional. pandas' default C parser uses
    # xstrtod, which accumulates digits with repeated multiplies and lands one ulp
    # off on values like "2.002" -- oracle.py's float() is correctly rounded, so
    # the two disagree on real data. round_trip uses the same correctly-rounded
    # strtod float() does. Spec §10 R2: a parse that is one ulp out is a bug to
    # fix, not a tolerance to widen.
    df = pd.read_csv(path, header=0, names=EXPECTED_HEADER,
                     dtype=_DTYPES, float_precision="round_trip")
    n_rows = len(df)
    if n_rows < 2:
        return []

    ts = df["timestamp"].to_numpy()
    count = np.rint(df.iloc[:, 1:1 + NUM_HEADS].to_numpy(dtype=np.float64)).astype(np.int64)
    torque = df.iloc[:, 1 + NUM_HEADS:1 + 2 * NUM_HEADS].to_numpy(dtype=np.float64)
    status = df.iloc[:, 1 + 2 * NUM_HEADS:].to_numpy(dtype=np.float64)

    # Spec §3: last_count_[h] after row i is always count[i][h], so the whole
    # transform is a one-row difference. Row 0 is the seed and emits nothing.
    delta = count[1:] - count[:-1]
    rows, heads = np.nonzero(delta)          # nonzero returns row-major order,
                                             # i.e. (row asc, head asc) -- the
                                             # emission order the C++ uses.
    if rows.size == 0:
        return []

    d = delta[rows, heads]
    cur = count[1:][rows, heads]
    tq = torque[1:][rows, heads]
    st = status[1:][rows, heads]
    is_reset = d < 0
    out_delta = np.where(is_reset, 0, d)

    # `% 2 != 0`, not `== 1`: Python floors and C++ truncates, so a negative
    # status gives 1 here and -1 there. Comparing against zero agrees in both.
    return [
        (int(heads[k]) + 1, ts[rows[k] + 1], int(cur[k]), float(tq[k]), float(st[k]),
         int(out_delta[k]), bool(int(st[k]) % 2 != 0), bool(out_delta[k] > 1),
         bool(is_reset[k]))
        for k in range(rows.size)
    ]


if __name__ == "__main__":
    print(len(extract(sys.argv[1])))
