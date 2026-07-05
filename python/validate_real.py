"""Cross-check the C++ core against the Python oracle on a REAL local file.

Usage:
    python3 validate_real.py <raw_day.csv> [cpp_events_out.csv]

Prints the oracle event count. If the C++ output CSV is given, asserts its
data-row count equals the oracle's. Real data is never committed (spec §12)."""
import sys
import oracle


def cpp_event_count(path):
    with open(path) as f:
        return sum(1 for i, _ in enumerate(f) if i > 0)  # minus header


def main():
    raw = sys.argv[1]
    events = oracle.extract(raw)
    print(f"oracle events: {len(events)}")
    if len(sys.argv) > 2:
        n = cpp_event_count(sys.argv[2])
        print(f"cpp events:    {n}")
        assert n == len(events), f"MISMATCH: cpp {n} vs oracle {len(events)}"
        print("MATCH")


if __name__ == "__main__":
    main()
