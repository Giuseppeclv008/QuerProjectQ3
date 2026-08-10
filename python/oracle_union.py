#!/usr/bin/env python3
"""Expected row count in the store across day-files (spec §7 oracle).

The store's identity is (machine_id, head_id, ts): one head closes at most once
per poll, so a timestamp names the observation. This script counts the distinct
(head_id, ts) pairs the extractor would emit over the given files — what the
store must hold after loading them all.

It used to count distinct (head_id, cap_seq), on the theory that days 16-24
"replayed" cap_seq ranges from days 1-15 and the store was right to drop them.
That theory was never tested and is false: of head 1's 23,851 day-17 closures
whose cap_seq collides with days 1-15, 18,721 carry a *different* torque. They
are distinct physical caps. The old oracle counted exactly the quantity the old
UNIQUE key left stable, so 81 of 81 benchmark runs reported "oracle-exact" while
34% of February and 63% of the three months were being discarded.

Because the day-files are contiguous and non-overlapping, this number now equals
the sum of the per-file event counts. The union is kept rather than a sum so an
overlapping or re-delivered file still yields the right answer.
"""
import sys

import oracle


def union_rows(paths):
    u = set()
    for p in paths:
        u |= {(e[0], e[1]) for e in oracle.extract(p)}   # (head_id, ts)
    return len(u)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: oracle_union.py <day.csv> [day2.csv ...]", file=sys.stderr)
        sys.exit(2)
    print(union_rows(sys.argv[1:]))
