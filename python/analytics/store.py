"""Read-only access to the cleaned cap_events store.

The toolkit never writes to the event store -- the C++ MAS owns it. Head count
is DISCOVERED, never assumed: our machine has 36 heads but the brief's example
dataset has 48, and WP5 requires the solution to generalise (spec §3.5).
"""
import duckdb


def connect(cfg):
    return duckdb.connect(cfg.store_path, read_only=True)


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
