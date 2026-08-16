"""Read-only access to the cleaned cap_events store.

The toolkit never writes to the event store -- the C++ MAS owns it. Head count
is DISCOVERED, never assumed: our machine has 36 heads but the brief's example
dataset has 48, and WP5 requires the solution to generalise (spec §3.5).
"""
import os

import duckdb


def store_fingerprint(cfg):
    """Identify the data a run was computed against.

    Nothing beyond a basename used to identify the store, so two stores
    differing 2.71x in row count (the retired cap_seq-keyed build vs the
    rebuilt one) were indistinguishable from a report or its trace -- which is
    exactly how a committed report and a doc came to quote different sigmas
    for the same measurement. Four numbers make "which store produced this?"
    a one-line check instead of archaeology.
    """
    con = connect(cfg)
    n, ts_min, ts_max, heads = con.execute(
        "SELECT COUNT(*), MIN(ts), MAX(ts), COUNT(DISTINCT head_id) "
        "FROM cap_events").fetchone()
    return {
        "store_path": os.path.basename(cfg.store_path),
        "rows": int(n),
        "ts_min": str(ts_min),
        "ts_max": str(ts_max),
        "distinct_heads": int(heads),
    }


def connect(cfg):
    """A read-only connection whose `cap_events` is the configured store.

    A directory means a Parquet store: `cap_events` becomes a view over its
    files, deduplicated on the event identity. The eight tools cannot tell the
    difference — none of their `FROM cap_events` change, which is what makes a
    comparison between the two backends a comparison of storage rather than of
    two different queries.
    """
    if not os.path.isdir(cfg.store_path):
        return duckdb.connect(cfg.store_path, read_only=True)

    glob = os.path.join(cfg.store_path, "*.parquet")
    import glob as _glob
    if not _glob.glob(glob):
        raise ValueError(
            f"{cfg.store_path} is a directory with no .parquet files in it; "
            "build one with `clean --format parquet` or point store_path at a "
            ".duckdb file")
    # :memory: cannot take read_only=True -- there is nothing to protect, the
    # database is created empty and holds only the view. What is actually
    # read-only is the data: read_parquet() never writes its files, and no tool
    # issues DML. The .duckdb branch above does pass read_only=True, where the
    # flag protects a real file.
    con = duckdb.connect(":memory:")
    # DISTINCT ON replaces the UNIQUE constraint the DuckDB backend enforces at
    # write time. On disjoint files it removes nothing and still costs a hash
    # aggregation over the whole corpus on every query: that is the price of
    # moving the dedup to the read side, and the reason bench/read_bench.py
    # exists.
    #
    # There is deliberately NO trailing ORDER BY, and the reasoning that once
    # put one here was wrong twice over.
    #
    # It claimed the sort made "which row of a duplicate group survives"
    # deterministic. EXPLAIN puts ORDER_BY *above* HASH_GROUP_BY, so it runs
    # after the group has already been collapsed and cannot influence the
    # choice. Sorting by the DISTINCT ON key could not break a tie in any case:
    # every row in a duplicate group compares equal on it. Adding the ORDER BY
    # did change the surviving row in a two-file experiment (999 -> 111), but
    # ASC and DESC agreed with each other -- so what moved was the plan, not a
    # tie-break, and nothing about it is a guarantee.
    #
    # And the guarantee was never needed. Duplicates arise only when a work
    # item is re-dispatched after a worker is declared dead, and those rows are
    # byte-identical by construction -- same input file, same extraction (see
    # DuckDbEventStore::merge_all and spec §3). Whichever row survives, the
    # content is the same.
    #
    # It cost 2.7-3.1x on the read path for that: 1.386 s against 0.516 s and
    # 1.254 s against 0.402 s on the month store, measured in
    # bench/parquet-comparison/decompose*.out.
    #
    # What was true, and still is: float aggregates are NOT bit-identical
    # against the DuckDB-native backend. STDDEV_SAMP/AVG/... run as a parallel
    # partial-aggregate/combine once DuckDB parallelises the scan, and combine
    # order follows neither scan order nor any ORDER BY. The 8-row fixture runs
    # single-threaded and agrees exactly, which is an artifact of its size, not
    # a property of this view -- test_backend_parity.py compares floats with a
    # relative tolerance for that reason.
    #
    # Tools that need ordered rows say so themselves (idle.py, anomaly.py,
    # torque.py all carry their own ORDER BY), so none of them depended on this.
    # read_parquet takes the glob as a SQL string literal, so the path is
    # spliced, not bound -- the plan's Global Constraints require it to go
    # through the same doubling `mas::sql_quote` applies on the C++ side. The
    # C++ stores are tested against a path containing an apostrophe; without
    # this the Python reader raised a ParserException on the identical path the
    # DuckDB backend opened without complaint, which is a backend asymmetry the
    # parity suite exists to catch.
    quoted = glob.replace("'", "''")
    con.execute(
        "CREATE VIEW cap_events AS "
        "SELECT DISTINCT ON (machine_id, head_id, ts) * "
        f"FROM read_parquet('{quoted}')")
    return con


def discover_heads(con, cfg):
    """The heads actually present for this machine. Never range(1, 37)."""
    rows = con.execute(
        "SELECT DISTINCT head_id FROM cap_events WHERE machine_id = ? ORDER BY head_id",
        [cfg.machine_id],
    ).fetchall()
    return [int(r[0]) for r in rows]


def _month_bounds(month):
    """'2026-02' -> ('2026-02-01', '2026-03-01') — half-open, so no leap-year math."""
    try:
        year, mon = (int(x) for x in month.split("-"))
    except ValueError:
        raise ValueError(f"unparseable period {month!r}; expected YYYY-MM")
    if not 1 <= mon <= 12:
        raise ValueError(f"unparseable period {month!r}; month out of range")
    start = f"{year:04d}-{mon:02d}-01"
    year_n, mon_n = (year + 1, 1) if mon == 12 else (year, mon + 1)
    return start, f"{year_n:04d}-{mon_n:02d}-01"


def period_clause(period):
    """Build a half-open ts filter. Accepts 'YYYY-MM', 'YYYY-MM..YYYY-MM', or None."""
    if period is None:
        return "TRUE", []
    if ".." in period:
        first, last = period.split("..", 1)
        start, _ = _month_bounds(first.strip())
        _, end = _month_bounds(last.strip())
        # A reversed range matched nothing and read as a confident "no
        # capping events" -- a typo producing a reassurance.
        if start >= end:
            raise ValueError(
                f"reversed period {period!r}: {first.strip()} is not before "
                f"{last.strip()}")
    else:
        start, end = _month_bounds(period.strip())
    return "ts >= ? AND ts < ?", [start, end]


def scope_clause(cfg, period):
    """The WHERE fragment every tool starts from: this machine, this period.

    Machine and period scoping live here, in one place, rather than being
    re-derived (and eventually forgotten) by each of the eight tools.
    """
    where, params = period_clause(period)
    return f"machine_id = ? AND {where}", [cfg.machine_id] + params
