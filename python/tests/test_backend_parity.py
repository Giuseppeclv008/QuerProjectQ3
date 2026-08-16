"""The same tools, the same data, two storage backends — they must agree.

This is the test that makes the comparison honest. None of the eight tools know
which backend they are reading, because `connect()` presents both as a table
named `cap_events`. A difference in results is therefore a defect in one
backend, not a difference in how it was queried.

What this does NOT cover, stated so nobody reads more into a green run than is
there: the Parquet fixture is written by DuckDB's own `COPY ... (FORMAT
PARQUET)`, not by the C++ `ParquetEventStore`. So a schema or type divergence
introduced by the C++ writer would not fail here -- it is caught, if at all, by
`ParquetEventStore.RoundTripsEveryColumn` on the C++ side. Closing the gap means
running a built binary from pytest, which nothing else in this suite does.

The fixture is also small enough to run single-threaded, which is why float
comparisons use a relative tolerance: at production row counts DuckDB
parallelises the aggregate and the combine order is not fixed. A fixture that
agreed exactly would be agreeing by accident of size.
"""
import math

import pytest

from analytics.config import Config
from analytics.tools.anomaly import anomalies
from analytics.tools.idle import idle_periods
from analytics.tools.overview import overview
from analytics.tools.speed import capping_speed
from analytics.tools.correlation import head_correlation
from analytics.tools.success import success_rates
from analytics.tools.torque import torque_stats
from analytics.tools.trend import trend


def values_close(a, b, rel_tol=1e-6, abs_tol=1e-9):
    """Compare tool `.values` trees: exact everywhere except floats.

    Counts, ids, strings and bools have no source of nondeterminism between
    the two backends and must still match exactly -- a tolerance there would
    hide real defects. Floats are different: DuckDB's STDDEV_SAMP/AVG/etc.
    run as a parallel partial-aggregate/combine once a table is large enough
    to parallelise, and combine order is not tied to scan order, so the two
    backends are not guaranteed bit-identical on float aggregates at
    production scale even with a deterministic ORDER BY on the Parquet view
    (see the comment on `connect()` in analytics/store.py). This project's
    tiny fixture happens to run single-threaded on both backends, so exact
    equality would pass here regardless -- the tolerance is what keeps this
    test meaningful once the fixture (or production data) is large enough to
    parallelise.
    """
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(
            values_close(a[k], b[k], rel_tol, abs_tol) for k in a
        )
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(
            values_close(x, y, rel_tol, abs_tol) for x, y in zip(a, b)
        )
    # Type-strict scalars: an int on one backend and a float (or bool) on the
    # other IS the divergence this suite hunts, and the old "either side is a
    # float" branch let values_close(5, 5.0) and values_close(1, True) pass.
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, float) != isinstance(b, float):
        return False
    if isinstance(a, float):
        return math.isclose(a, b, rel_tol=rel_tol, abs_tol=abs_tol)
    return a == b


TOOLS = [
    ("overview", lambda c: overview(c, period="2026-02")),
    ("success_overall", lambda c: success_rates(c, period="2026-02", by="overall")),
    ("success_head", lambda c: success_rates(c, period="2026-02", by="head")),
    ("torque", lambda c: torque_stats(c, period="2026-02")),
    ("trend", lambda c: trend(c, period="2026-02", by="day")),
    ("head_correlation", lambda c: head_correlation(c, period="2026-02")),
    ("speed", lambda c: capping_speed(c, period="2026-02", bucket="hour")),
    ("idle", lambda c: idle_periods(c, period="2026-02")),
    ("anomalies", lambda c: anomalies(c, period="2026-02")),
]


@pytest.mark.parametrize("name,call", TOOLS, ids=[t[0] for t in TOOLS])
def test_both_backends_agree(name, call, tiny_store, tiny_store_parquet):
    duck = call(Config(store_path=tiny_store, machine_id="MCC"))
    pq = call(Config(store_path=tiny_store_parquet, machine_id="MCC"))
    assert duck.status == pq.status, f"{name}: status differs"
    assert values_close(duck.values, pq.values), (
        f"{name}: values differ beyond float tolerance\nduck={duck.values!r}\npq={pq.values!r}"
    )


def test_parquet_view_deduplicates_a_redispatched_file(tmp_path, tiny_store_parquet):
    """Two files with identical events must present as one set of events.

    This is what replaces the UNIQUE constraint: the write side cannot prevent
    the duplicate, so the read side removes it.
    """
    import shutil
    from analytics.store import connect
    src = sorted(p for p in __import__("pathlib").Path(tiny_store_parquet).glob("*.parquet"))
    assert src, "fixture produced no parquet files"
    shutil.copy(src[0], src[0].with_name("redispatched.parquet"))
    con = connect(Config(store_path=tiny_store_parquet, machine_id="MCC"))
    n = con.execute("SELECT COUNT(*) FROM cap_events").fetchone()[0]
    assert n == 8, f"expected the 8 fixture events once each, got {n}"


def test_both_backends_open_a_path_containing_a_quote(tmp_path, tiny_store, tiny_store_parquet):
    """The asymmetry this suite exists to catch, in its smallest form.

    The DuckDB path is passed to `duckdb.connect()` as a value; the Parquet
    path is spliced into `read_parquet('...')` as a SQL literal. Before the
    escape, that difference was visible to the operator: the same directory
    name opened on one backend and raised a ParserException on the other. The
    C++ stores are tested on a quoted path (`ParquetEventStore` and
    `ParquetExport` both), so this is the Python side of a rule the plan states
    globally.
    """
    import shutil
    from analytics.store import connect

    duck_dir = tmp_path / "o'brien duck"
    duck_dir.mkdir()
    duck_copy = duck_dir / "tiny.duckdb"
    shutil.copy(tiny_store, duck_copy)
    duck_con = connect(Config(store_path=str(duck_copy), machine_id="MCC"))
    assert duck_con.execute("SELECT COUNT(*) FROM cap_events").fetchone()[0] == 8

    pq_dir = tmp_path / "o'brien parquet"
    shutil.copytree(tiny_store_parquet, pq_dir)
    pq_con = connect(Config(store_path=str(pq_dir), machine_id="MCC"))
    assert pq_con.execute("SELECT COUNT(*) FROM cap_events").fetchone()[0] == 8


def test_empty_parquet_directory_fails_loudly(tmp_path):
    from analytics.store import connect
    empty = tmp_path / "empty_store"
    empty.mkdir()
    with pytest.raises(ValueError, match="no .parquet files"):
        connect(Config(store_path=str(empty), machine_id="MCC"))
