"""Read-only access to the cleaned cap_events store.

The toolkit never writes to the event store -- the C++ MAS owns it. Head count
is DISCOVERED, never assumed: our machine has 36 heads but the brief's example
dataset has 48, and WP5 requires the solution to generalise (spec §3.5).
"""
import os

import duckdb


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
    con = duckdb.connect(":memory:")
    # DISTINCT ON is what replaces the UNIQUE constraint the DuckDB backend
    # enforces at write time. On disjoint files it removes nothing but still
    # costs a full sort of the corpus on every query -- see the ORDER BY note
    # below -- the price of moving the dedup to the read side, and the reason
    # bench/read_bench.py exists.
    #
    # The trailing ORDER BY is load-bearing for correctness, not just
    # numerics: DuckDB implements a bare DISTINCT ON as HASH_GROUP_BY with
    # first(), so without an explicit ORDER BY, *which* row of a duplicate
    # group survives is genuinely undefined (this is documented Postgres
    # DISTINCT ON behaviour too, which DuckDB's syntax mirrors). Ordering by
    # (machine_id, head_id, ts) makes "first" mean "earliest by event
    # identity", so a redispatched file's duplicate rows resolve the same way
    # every time.
    #
    # What it does NOT guarantee: bit-identical floating-point aggregates
    # against the DuckDB-native backend. STDDEV_SAMP/AVG/etc. still run as a
    # parallel partial-aggregate/combine once DuckDB parallelises the scan
    # (its default 8 threads, triggered by table size), and combine order is
    # not tied to scan order -- ORDER BY sorts the rows fed to DISTINCT ON,
    # not the order the aggregate's partial results are merged in. On this
    # project's 8-row test fixture both backends happen to run single-
    # threaded, so results are bit-identical there, but that is an artifact
    # of fixture size, not a property this view provides. At production row
    # counts, expect float aggregates to agree only to a relative tolerance,
    # not exactly -- see test_backend_parity.py's comparison helper.
    #
    # Cost: EXPLAIN shows this ORDER BY adds a full ORDER_BY operator on top
    # of the HASH_GROUP_BY, not just "a hash aggregation" -- an O(n log n)
    # sort of the whole matched corpus, on every query, regardless of the
    # period filter (predicate/period pushdown does not reach past the sort).
    # Measured ~4x slower than the unordered view on a 2M-row table (0.123s
    # vs 0.031s). This is a real, non-trivial cost that the read-performance
    # benchmark (bench/read_bench.py, and Tasks 5-6) must account for.
    con.execute(
        "CREATE VIEW cap_events AS "
        "SELECT DISTINCT ON (machine_id, head_id, ts) * "
        f"FROM read_parquet('{glob}') "
        "ORDER BY machine_id, head_id, ts")
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
