import csv
import os
import oracle


def _write_raw(path, rows):
    """rows: list of (ts, head1_count). Only head 1 varies; 109 columns."""
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        header = ["timestamp"] + [f"c{i}" for i in range(108)]
        w.writerow(header)
        for ts, c in rows:
            counts = [c] + [0.0] * 35
            torque = [2.0] + [0.0] * 35
            status = [2.0] + [0.0] * 35
            w.writerow([ts] + counts + torque + status)


def test_oracle_counts_increments_and_dedups_held(tmp_path):
    path = os.path.join(tmp_path, "raw.csv")
    _write_raw(path, [("t0", 100), ("t1", 100), ("t2", 101), ("t3", 104)])
    events = oracle.extract(path)
    assert len(events) == 2                 # t2 (+1) and t3 (+3)
    assert events[0][5] == 1                # delta of first event
    assert events[1][5] == 3 and events[1][7] is True   # aggregated
