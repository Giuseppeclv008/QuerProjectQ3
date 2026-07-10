#!/usr/bin/env python3
"""Expected UNIQUE(head_id, cap_seq) row count across day-files (spec §7 oracle).

The real month's Count counter reset mid-day-16 (discovered at the Task 5
gate): days 16-24 replay cap_seq ranges already seen in days 1-15, and the
store's UNIQUE(machine_id, head_id, cap_seq) constraint dedupes the replays.
So for multi-day volumes the expected row count is the union of
(head_id, cap_seq) pairs, not the sum of per-file event counts
(21,872,663 events vs 14,372,237 rows over the 28-day month).
"""
import sys

import oracle

_KEY = 10 ** 12  # cap_seq stays far below this; packs (head_id, cap_seq) into one int


def union_rows(paths):
    u = set()
    for p in paths:
        u |= {e[0] * _KEY + int(e[2]) for e in oracle.extract(p)}
    return len(u)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: oracle_union.py <day.csv> [day2.csv ...]", file=sys.stderr)
        sys.exit(2)
    print(union_rows(sys.argv[1:]))
