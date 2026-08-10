"""Cross-check the C++ core against the Python oracle on a REAL local file.

Usage:
    python3 validate_real.py <raw_day.csv> [cpp_events_out.csv]

Prints the oracle event count. If the C++ output CSV is given, compares every
field of every event, not just how many there are. Real data is never committed
(spec §12).

Why fields and not counts: this script used to assert only
`len(cpp) == len(oracle)`. is_fault, status, torque and delta do not change how
many events are emitted, so a disagreement in any of them was invisible — and
one was live for three plans (oracle.py classified rejects as `status == 65`
while the C++ used the bitmask). A gate that can only see cardinality cannot see
a semantics bug.
"""
import csv
import sys

import oracle

_FIELDS = ("head_id", "ts", "cap_seq", "app_torque", "status",
           "delta", "is_fault", "aggregated", "reset")
_MAX_SHOWN = 10


def read_cpp(path):
    """Parse CsvEventStore output into oracle-shaped tuples."""
    out = []
    with open(path, newline="") as f:
        reader = csv.reader(f)
        next(reader)   # machine_id,head_id,ts,cap_seq,app_torque,status,...
        for r in reader:
            out.append((int(r[1]), r[2], int(r[3]), float(r[4]), float(r[5]),
                        int(r[6]), r[7] == "1", r[8] == "1", r[9] == "1"))
    return out


def compare(cpp, ref):
    """Return a list of human-readable differences, empty when identical."""
    diffs = []
    if len(cpp) != len(ref):
        diffs.append(f"count: cpp {len(cpp)} vs oracle {len(ref)}")
    for i, (a, b) in enumerate(zip(cpp, ref)):
        if a == b:
            continue
        bad = [f"{name}: cpp={x!r} oracle={y!r}"
               for name, x, y in zip(_FIELDS, a, b) if x != y]
        diffs.append(f"event {i}: " + "; ".join(bad))
        if len(diffs) >= _MAX_SHOWN:
            diffs.append("... further differences suppressed")
            break
    return diffs


def main():
    if len(sys.argv) < 2:
        print("usage: validate_real.py <raw_day.csv> [cpp_events_out.csv]",
              file=sys.stderr)
        return 2
    raw = sys.argv[1]
    ref = oracle.extract(raw)
    print(f"oracle events: {len(ref)} (rows skipped: {oracle.extract.skipped})")
    if len(sys.argv) <= 2:
        return 0

    cpp = read_cpp(sys.argv[2])
    print(f"cpp events:    {len(cpp)}")
    diffs = compare(cpp, ref)
    if diffs:
        print("MISMATCH:")
        for d in diffs:
            print("  " + d)
        return 1
    print(f"MATCH: {len(ref)} events, all {len(_FIELDS)} fields")
    return 0


if __name__ == "__main__":
    sys.exit(main())
