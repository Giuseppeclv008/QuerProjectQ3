"""Read-only access to the cleaned cap_events store.

The toolkit never writes to the event store -- the C++ MAS owns it. Head count
is DISCOVERED, never assumed: our machine has 36 heads but the brief's example
dataset has 48, and WP5 requires the solution to generalise (spec §3.5).
"""
import os

import duckdb


def store_fingerprint(cfg):
    """Identify the data a run was computed against.

    A basename identifies a file, not its contents: two builds of the same
    machine's events can differ severalfold in row count and still be
    indistinguishable from a report or its trace, which is how a committed
    report and a doc come to quote different figures for the same measurement.
    Four numbers make "which store produced this?" a one-line check instead of
    archaeology.
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
    # Deliberately no trailing ORDER BY. It could not make "which row of a
    # duplicate group survives" deterministic -- the sort runs after the group
    # is collapsed, and every row of a group compares equal on the DISTINCT ON
    # key -- and no such guarantee is needed: duplicates come from a work item
    # re-dispatched after a worker was declared dead, so they are byte-identical
    # by construction (DuckDbEventStore::merge_all, spec §3). It cost 2.7-3.1x
    # on the read path, measured in bench/parquet-comparison/.
    #
    # Float aggregates are not bit-identical against the DuckDB-native backend
    # either way: STDDEV_SAMP/AVG/... run as a parallel partial-aggregate/
    # combine, and combine order follows neither scan order nor any ORDER BY --
    # which is why test_backend_parity.py compares floats with a relative
    # tolerance. Tools that need ordered rows carry their own ORDER BY
    # (idle.py, anomaly.py, torque.py).

    # read_parquet takes the glob as a SQL string literal, so the path is
    # spliced, not bound: it goes through the same quote doubling
    # `mas::sql_quote` applies on the C++ side, which the parity suite checks
    # against a path containing an apostrophe.
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
