#!/usr/bin/env python3
"""Time the three canned reports against each storage backend.

The write side is only half the question. The Parquet backend moves dedup to
read time, so a write saving can be given back on every query. This measures
that, and is allowed to conclude that DuckDB's write-time index is the cheaper
place to pay.

The Parquet-backed `cap_events` view (`analytics.store.connect`) carries a
trailing `ORDER BY machine_id, head_id, ts` on top of its `DISTINCT ON`
dedup: DuckDB compiles a bare `DISTINCT ON` to HASH_GROUP_BY + first(), so
without an explicit order "which row survives a duplicate group" is
undefined. That ORDER BY is a real ORDER_BY operator, not just the hash
aggregation -- an O(n log n) sort of the whole matched corpus, on every
query, and predicate/period pushdown does not reach past it (so a query
scoped to one period still sorts the entire table). Measured ~4x slower than
the unordered view on a 2M-row table (0.123s vs 0.031s). Every "parquet" row
this script produces has that sort baked into it -- it is not an artifact of
this harness, it is the cost `analytics.store.connect` actually pays, but
anyone reading `read_results.csv` needs to know it's in there.

usage: read_bench.py <duckdb-store> <parquet-dir> [repeats]
"""
import csv
import json
import subprocess
import sys
import tempfile
import time

REPORTS = ("kpi", "drift", "anomalies")
OUT = "bench/read_results.csv"


def run_report(store, kind, out_dir):
    # analytics.cli's --config takes a file path only (checked: no "-"/stdin
    # handling in cli.py -- load_config() does a plain `open(path)`), so the
    # config goes to a temp file instead of stdin.
    with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False) as fh:
        json.dump({"store_path": store, "machine_id": "MCC"}, fh)
        cfg_path = fh.name
    t0 = time.perf_counter()
    p = subprocess.run(
        [sys.executable, "-m", "analytics.cli", "report", kind,
         "--config", cfg_path, "--out", out_dir],
        cwd="python", capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit(f"{kind} on {store} failed:\n{p.stdout}{p.stderr}")
    return time.perf_counter() - t0


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    duck, pq = sys.argv[1], sys.argv[2]
    reps = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    rows = []
    for rep in range(1, reps + 1):
        for backend, store in (("duckdb", duck), ("parquet", pq)):
            for kind in REPORTS:
                s = run_report(store, kind, f"/tmp/readbench-{backend}")
                rows.append({"backend": backend, "report": kind,
                             "repeat": rep, "seconds": round(s, 3)})
                print(f"{backend:<8}{kind:<11}rep {rep}: {s:.3f}s")
    with open(OUT, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["backend", "report", "repeat", "seconds"])
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
