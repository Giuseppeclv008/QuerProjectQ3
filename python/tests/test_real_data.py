"""Real-data gate. Skipped unless events_3mo.duckdb exists (build it with
scripts/build_store.sh). This is the analytics equivalent of the oracle checks
that guarded Plans 1-5.
"""
import glob
import os

import duckdb
import pytest

import oracle_kpi
from analytics.config import Config
from analytics.status import REJECT_SQL
from analytics.tools.overview import overview
from analytics.tools.success import success_rates
from analytics.tools.torque import torque_stats
from analytics.tools.trend import trend

# Anchored on this source file, not the CWD: the CWD-relative
# "../events_3mo.duckdb" resolved to the repo's PARENT when pytest ran from
# the repo root, so the gate was unsatisfiable there even with the store
# built -- the C++ twins got exactly this fix in 226f04a and the Python side
# was missed. The store lives in the repo root (where scripts/demo.sh
# defaults to and scripts/build_store.sh is documented to write).
STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     os.pardir, os.pardir, "events_3mo.duckdb")
pytestmark = pytest.mark.skipif(
    not os.path.exists(STORE), reason=f"{STORE} absent; run scripts/build_store.sh"
)


@pytest.fixture
def cfg():
    return Config(store_path=STORE, machine_id="MCC")


def test_february_matches_the_numbers_measured_during_brainstorming(cfg):
    r = overview(cfg, period="2026-02")
    v = r.values
    # These are the counts measured directly off the raw CSV for 2026-02-01,
    # scaled to the month by the store. The per-day figures were:
    #   427,643 real caps | 337,772 no-load | 4 faults
    # Here we assert the invariants that must hold at any scale.
    assert v["capping_operations"] > 0
    assert v["no_load_cycles"] > 0
    assert v["successful"] + v["failed"] <= v["capping_operations"]
    assert v["heads"] == list(range(1, 37))       # this machine has 36


def test_success_rate_is_high_and_no_load_is_never_counted_as_failure(cfg):
    r = success_rates(cfg, period="2026-02", by="overall")
    # The machine is healthy: ~99.999%. If a regression ever counts no-load
    # cycles as failures, this collapses to ~56% and the test catches it.
    assert r.values["success_rate"] > 0.99


def test_mean_torque_sits_in_the_measured_band(cfg):
    r = torque_stats(cfg, period="2026-02", outcome="successful")
    # Measured on real data: mean 1.998, min 1.885, max 2.317. If zero-torque
    # no-load cycles ever leak in, the mean craters and this fails.
    assert 1.9 < r.values["mean"] < 2.1


def test_drift_runs_across_all_three_months(cfg):
    r = trend(cfg, period="2026-02..2026-04", signal="torque", by="day")
    assert r.status == "ok"
    assert len(r.values["drift"]) == 36


def test_toolkit_agrees_with_the_independent_oracle_on_one_day(cfg):
    """The toolkit reads the store via SQL; the oracle reads the raw CSV directly.
    They share no code. If they disagree, one of them is wrong."""
    day = "2026-02-01"
    matches = glob.glob(f"../telemetry_*/*{day}.csv")
    if not matches:
        pytest.skip(f"raw CSV for {day} not extracted")

    expected = oracle_kpi.kpis(matches[0])

    # The oracle re-derives these from the raw CSV. They must reproduce exactly what
    # was measured at design time (spec §3.1) -- if they do not, the semantics moved.
    assert expected["successful"] == 427643
    assert expected["failed"] == 4
    assert expected["no_load_cycles"] == 337772
    assert expected["capping_operations"] == 427643 + 155 + 4   # every closure with torque > 0

    # The store covers this day plus the rest of the month, so the month's counts must
    # contain the day's. This is what ties the toolkit's SQL to the independent oracle.
    r = overview(cfg, period="2026-02")
    assert r.status == "ok"
    assert r.values["capping_operations"] >= expected["capping_operations"]
    assert r.values["no_load_cycles"] >= expected["no_load_cycles"]


def test_the_stored_reject_flag_and_the_sql_rule_agree():
    """Cross-language parity, asserted instead of claimed.

    `is_fault` is written at extraction time by C++ (`CapEvent::is_reject`);
    REJECT_SQL re-derives the verdict from `status` at query time. README,
    analytics-methods and status.py all state the two are the same rule --
    this row-by-row comparison over the real store is the test behind the
    sentence. Divergence is only reachable off the store's domain (a
    non-finite status reads reject in C++ and errors in SQL), and the row
    policy keeps such cells out of the store; if either half moves, this
    counts the rows that disagree.
    """
    con = duckdb.connect(STORE, read_only=True)
    try:
        mismatched = con.execute(
            f"SELECT COUNT(*) FROM cap_events WHERE ({REJECT_SQL}) <> is_fault"
        ).fetchone()[0]
    finally:
        con.close()
    assert mismatched == 0
