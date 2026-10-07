#!/usr/bin/env python
"""Where does the Parquet read penalty come from -- the format, or the dedup?

A whole-view ratio invites the wrong conclusion, that the columnar format is
slow. This peels the read view apart one layer at a time on a single aggregate
over the whole month, so the cost lands where it belongs. The 5.2x that
prompted it was measured through the ORDER BY the last layer below records as
withdrawn on 2026-08-14; without that sort the penalty is 2.41x.
docs/bench/results.md carries both, with the box and the date.

usage: decompose.py <duckdb store> <parquet dir> [repeats]
"""
import glob
import os
import statistics as st
import sys
import time

import duckdb

Q = ("SELECT head_id, COUNT(*), AVG(app_torque), "
     "SUM(CASE WHEN is_fault THEN 1 ELSE 0 END) "
     "FROM cap_events WHERE machine_id='MCC' GROUP BY head_id ORDER BY head_id")


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    store, pqdir = sys.argv[1], sys.argv[2]
    reps = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    files = sorted(glob.glob(os.path.join(pqdir, "*.parquet")))
    if not files:
        sys.exit(f"no .parquet files in {pqdir}")
    # Doubled quotes, like store.py and mas::sql_quote: read_parquet takes the
    # glob as a SQL literal. This script measures the shipped view, so it has to
    # build that view the way the shipped code does -- a decomposition that
    # fails on a path the real reader opens is measuring something else.
    g = os.path.join(pqdir, "*.parquet").replace("'", "''")

    layers = [
        ("duckdb native table", None),
        ("parquet, plain scan, no dedup",
         f"CREATE VIEW cap_events AS SELECT * FROM read_parquet('{g}')"),
        ("parquet + DISTINCT ON (the shipped view)",
         "CREATE VIEW cap_events AS SELECT DISTINCT ON (machine_id, head_id, ts) * "
         f"FROM read_parquet('{g}')"),
        ("parquet + DISTINCT ON + ORDER BY (removed 2026-08-14)",
         "CREATE VIEW cap_events AS SELECT DISTINCT ON (machine_id, head_id, ts) * "
         f"FROM read_parquet('{g}') ORDER BY machine_id, head_id, ts"),
    ]

    pq_bytes = sum(os.path.getsize(f) for f in files)
    db_bytes = os.path.getsize(store)
    print(f"parquet store {pq_bytes/1e6:.1f} MB in {len(files)} files ({pq_bytes} B)")
    print(f"duckdb  store {db_bytes/1e6:.1f} MB ({db_bytes} B)")
    print(f"duckdb / parquet on disk: {db_bytes/pq_bytes:.3f}x\n")

    med = {}
    for label, view in layers:
        con = (duckdb.connect(store, read_only=True) if view is None
               else duckdb.connect(":memory:"))
        if view:
            con.execute(view)
        ts = []
        for _ in range(reps):
            t0 = time.perf_counter()
            con.execute(Q).fetchall()
            ts.append(time.perf_counter() - t0)
        con.close()
        med[label] = st.median(ts)
        print(f"{label:<46} median {med[label]:6.3f}s   "
              f"runs {', '.join(f'{t:.3f}' for t in ts)}")

    k = list(med)
    print(f"\nplain scan / native                {med[k[1]]/med[k[0]]:.3f}x")
    print(f"DISTINCT ON / plain scan           {med[k[2]]/med[k[1]]:.3f}x")
    print(f"shipped view / native              {med[k[2]]/med[k[0]]:.3f}x")
    print(f"[withdrawn] ORDER BY / DISTINCT ON {med[k[3]]/med[k[2]]:.3f}x")


if __name__ == "__main__":
    main()
