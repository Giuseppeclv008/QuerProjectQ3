#!/usr/bin/env python3
"""Time the three canned reports against each storage backend.

The write side is only half the question. The Parquet backend moves dedup to
read time, so a write saving can be given back on every query. This measures
that, and is allowed to conclude that DuckDB's write-time index is the cheaper
place to pay.

The Parquet-backed `cap_events` view (`analytics.store.connect`) pays a
`DISTINCT ON` hash aggregation over the whole corpus on every query, which the
DuckDB backend pays once at write time as a UNIQUE index probe. That is the
comparison.

It used to carry a trailing `ORDER BY machine_id, head_id, ts` as well, and
every number in earlier revisions of `read_results.csv` has that sort baked in
-- 2.7-3.1x of the Parquet read cost. It was removed once the justification for
it turned out to be false: the sort runs above the HASH_GROUP_BY, so it cannot
decide which row of a duplicate group survives, and duplicates are byte-identical
anyway. See the comment in `python/analytics/store.py`.

Before timing anything, this script queries both stores for the row count
scoped to --machine-id and refuses to run if either is zero, or if the two
disagree with each other. A benchmark that returns fast, plausible-looking
numbers because it was pointed at the wrong machine_id (and so matched
almost nothing) is worse than one that crashes: it would report Parquet
reads as cheap for the wrong reason, and that number would end up in the
project's own benchmark documentation. This project has already been burned
twice by measurements that were green for the wrong reason -- an oracle that
counted the one quantity a defect left stable, and a heartbeat test that
passed because idle ticks satisfied its threshold -- so this check is not
optional and cannot be skipped by a flag.

usage: read_bench.py <duckdb-store> <parquet-dir> [repeats] [--machine-id ID]
"""
import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile
import time

try:
    import duckdb  # noqa: F401 -- import-time guard. analytics.store needs this
    # module too, and the `report` subprocess spawned below runs under this
    # same sys.executable, so a missing duckdb here means every report call
    # would fail the same way, just later and with a less legible traceback.
except ImportError:
    sys.exit(
        f"error: no 'duckdb' module importable under {sys.executable}\n"
        "run this script with the project venv, e.g.:\n"
        "    .venv/bin/python bench/read_bench.py <duckdb-store> <parquet-dir>"
    )

# `bench/read_results.csv` below and the `cwd="python"` subprocess call in
# run_report() both assume the caller's cwd is the repo root; this insertion
# is the one thing in this file that must work regardless of cwd, since it's
# what makes `from analytics... import ...` resolve inside *this* process
# for the pre-flight scope check.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))

REPORTS = ("kpi", "drift", "anomalies")
OUT = "bench/read_results.csv"


def _row_count(store, machine_id):
    """Rows visible through the same `cap_events` abstraction the reports query.

    Goes through `analytics.store.connect` itself, not a reimplementation of
    its SQL, so a duckdb-file store and a parquet-directory store are counted
    through exactly the mechanism that gets timed below -- including the
    parquet side's DISTINCT ON dedup. (It said "and ORDER BY" until the sort was
    removed, sixty lines under the docstring that explains the removal.)
    """
    from analytics.config import Config
    from analytics.store import connect

    cfg = Config(store_path=store, machine_id=machine_id)
    try:
        con = connect(cfg)
    except ValueError as exc:
        sys.exit(f"cannot open store {store!r}: {exc}")
    n = con.execute(
        "SELECT COUNT(*) FROM cap_events WHERE machine_id = ?", [machine_id]
    ).fetchone()[0]
    con.close()
    return n


def check_scope(duck, pq, machine_id):
    """Refuse to benchmark an empty or mismatched store pair.

    Two failure modes, both fatal, neither produces a CSV row:
      - either store has 0 rows for machine_id (near-certainly the store was
        built with a different machine_id than the one given here)
      - the two stores disagree on the count (they aren't the same dataset,
        so their read timings would not be comparable)
    """
    duck_n = _row_count(duck, machine_id)
    pq_n = _row_count(pq, machine_id)
    for label, store, n in (("duckdb", duck, duck_n), ("parquet", pq, pq_n)):
        if n == 0:
            sys.exit(
                f"refusing to benchmark: {label} store {store!r} has 0 rows for "
                f"machine_id={machine_id!r}. The store's machine_id must match "
                f"--machine-id (default 'MCC') -- it was probably built with a "
                f"different one."
            )
    if duck_n != pq_n:
        sys.exit(
            f"refusing to benchmark: row counts disagree for machine_id={machine_id!r} "
            f"-- {duck!r} has {duck_n}, {pq!r} has {pq_n}. These stores are not "
            f"the same dataset; their read timings would not be comparable."
        )
    print(f"scope check: machine_id={machine_id!r} -> {duck_n} rows in both stores")
    return duck_n


def run_report(store, kind, out_dir, machine_id):
    # analytics.cli's --config takes a file path only (checked: no "-"/stdin
    # handling in cli.py -- load_config() does a plain `open(path)`), so the
    # config goes to a temp file instead of stdin.
    with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False) as fh:
        json.dump({"store_path": store, "machine_id": machine_id}, fh)
        cfg_path = fh.name
    try:
        t0 = time.perf_counter()
        p = subprocess.run(
            [sys.executable, "-m", "analytics.cli", "report", kind,
             "--config", cfg_path, "--out", out_dir],
            cwd="python", capture_output=True, text=True)
        if p.returncode != 0:
            sys.exit(f"{kind} on {store} failed:\n{p.stdout}{p.stderr}")
        return time.perf_counter() - t0
    finally:
        # delete=False above means nothing else removes it, and a full sweep is
        # 18 report runs -- 18 stray JSON files per sweep.
        os.unlink(cfg_path)


def main():
    parser = argparse.ArgumentParser(
        description="Time the three canned reports against each storage backend.")
    parser.add_argument("duckdb_store")
    parser.add_argument("parquet_dir")
    parser.add_argument("repeats", nargs="?", type=int, default=3)
    parser.add_argument(
        "--machine-id", default="MCC",
        help="must match the machine_id both stores were built with (default: "
             "'MCC', Config's default). A mismatch silently scopes every query "
             "to ~0 rows instead of failing loudly -- which is exactly what the "
             "pre-flight scope check exists to catch instead.")
    parser.add_argument(
        "--force", action="store_true",
        help="overwrite an existing bench/read_results.csv (refused otherwise)")
    args = parser.parse_args()
    duck, pq, reps, machine_id = (
        args.duckdb_store, args.parquet_dir, args.repeats, args.machine_id)

    # Same clobber guard as run_bench.sh and run_bench_cuda.py: this is the one
    # harness the fix did not reach, and read_results.csv is a committed
    # artifact the docs quote six figures from.
    if os.path.exists(OUT) and not args.force:
        sys.exit(f"refusing to overwrite {OUT}; pass --force or move it aside")

    check_scope(duck, pq, machine_id)

    rows = []
    for rep in range(1, reps + 1):
        for backend, store in (("duckdb", duck), ("parquet", pq)):
            for kind in REPORTS:
                s = run_report(store, kind, f"/tmp/readbench-{backend}", machine_id)
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
