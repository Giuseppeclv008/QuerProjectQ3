# Analytics Foundation — Implementation Plan (Plan 6)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct the inverted status semantics in the C++ core, then build the WP2 deterministic analytics toolkit — seven pure-function tools over the cleaned DuckDB store, each returning typed results with provenance — so that Plan 7's report agent has reliable tools to call.

**Architecture:** A new `analytics/` Python package reads the cleaned `cap_events` DuckDB store read-only. Every tool is a pure function: parameterised SQL in, a typed `ToolResult` out carrying values + provenance + status. No tool hard-codes a head count, a path, or a torque band — all come from config. The C++ MAS is unchanged except for a semantics fix (comments + two predicates); the store schema does not change at all.

**Tech Stack:** Python 3 (duckdb, pandas, numpy, pytest), C++20 (existing core), DuckDB.

**Spec:** `docs/superpowers/specs/2026-07-11-agentic-analytics-reporting-design.md` — semantics authority. Read §3 (data semantics) and §5 (tool table) before starting.

## Global Constraints

- **Branch:** `feat/agentic-analytics` (already created, spec already committed as `d5cad09`).
- **Status semantics (spec §3.2, locked — every tool obeys these):**
  - successful cap := `status == 0 AND app_torque > 0`
  - failed cap := `status == 65`
  - no-load cycle := `status == 2 AND app_torque == 0`
  - a **capping operation** := any closure with `app_torque > 0` (no-load cycles are EXCLUDED from every success denominator)
  - success rate := successful / (successful + failed)
- **Never hard-code 36 heads (spec §3.5).** Head count is discovered from the store. Any tool that assumes 36 is a bug.
- **Never hard-code paths (WP5).** Store path, torque bands, thresholds all come from config.
- **Tool errors are values, not exceptions (spec §8).** A tool returns `ToolResult(status="insufficient_data"|"error")`; it does not raise for degenerate data.
- **Provenance is mandatory.** Every `ToolResult` carries the period, rows scanned, and filters applied.
- **Python style:** match `python/oracle.py` — module docstring explaining the *why*, stdlib-first, no classes where a function does.
- **Tests:** `pytest python/tests/ -v` must be green at every commit. C++ suite `cd build && ctest` must stay green (68 tests).
- **Commits:** conventional style, each ending with:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- **DuckDB is opened READ_ONLY** by all analytics code. The toolkit never writes to the event store.

## Deviation from spec §5.4 (deliberate, approved)

The spec put **capping speed** in the C++ extractor (an "incremental average" per the brief's WP1). This plan computes it in **SQL in the toolkit** (Task 8) instead. It is the same number — caps per time bucket over events we already persist — but needs no schema column, no migration, and **no reprocessing of the 89 day-files**. This resolves spec §12 open question 2. Update the spec's §5.4 and §12 in Task 12.

**Validation checks** (also WP1 in the spec) land in `overview()` (Task 5) rather than the reader, for the same reason: the data to validate is already in the store.

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `core/include/mas/domain/CapEvent.hpp` | modify | fix inverted comment; add `is_no_load()` / `is_successful_cap()` predicates |
| `tests/test_cap_event.cpp` | create | pin the corrected semantics |
| `python/requirements.txt` | create | pin duckdb + existing deps |
| `python/analytics/__init__.py` | create | package marker, re-export tool functions |
| `python/analytics/config.py` | create | `Config` dataclass, TOML/JSON load, validation, defaults |
| `python/analytics/store.py` | create | read-only DuckDB connection, `discover_heads()`, `period_clause()` |
| `python/analytics/result.py` | create | `ToolResult` + `Provenance` types |
| `python/analytics/tools/overview.py` | create | `overview()` — counts, time range, validation checks |
| `python/analytics/tools/success.py` | create | `success_rates()` |
| `python/analytics/tools/torque.py` | create | `torque_stats()` |
| `python/analytics/tools/speed.py` | create | `capping_speed()` |
| `python/analytics/tools/idle.py` | create | `idle_periods()` |
| `python/analytics/tools/anomaly.py` | create | `anomalies()` — threshold + MAD deviation |
| `python/analytics/tools/trend.py` | create | `trend()` — rolling stats + Mann-Kendall drift |
| `python/analytics/tools/correlation.py` | create | `head_correlation()` |
| `python/tests/conftest.py` | create | `tiny_store` fixture — hand-built DuckDB with known values |
| `python/tests/test_*.py` | create | one test module per tool |
| `docs/validation-log.md` | modify | real-data results for Plan 6 |

---

### Task 1: Correct the status semantics in the C++ core

The shipped comment says `0 = idle/held, 2 = OK cap`. Measured at closure, that is inverted (spec §3.1). No behavior changes — `is_fault_status` was already correct — but the wrong comment is how a future reader builds a wrong KPI, and the two new predicates give the semantics a single home.

**Files:**
- Modify: `core/include/mas/domain/CapEvent.hpp:9-13`
- Create: `tests/test_cap_event.cpp`
- Modify: `CMakeLists.txt` (add the new test file to `unit_tests`)

**Interfaces:**
- Produces: `mas::is_successful_cap(double status, double torque) -> bool`, `mas::is_no_load(double status, double torque) -> bool`. Plan 7 and the SQL in Tasks 5–11 mirror these exact definitions.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cap_event.cpp`:

```cpp
#include <gtest/gtest.h>
#include "mas/domain/CapEvent.hpp"

// Semantics measured at closure over 2026-02-01 (765,711 closures), spec §3.1:
//   status 0 + torque > 0  -> real capping operation (427,643/day, mean 1.998 Nm)
//   status 2 + torque == 0 -> No-Load cycle          (337,772/day)
//   status 65              -> fault                  (4/day)
// The pre-existing header comment had 0 and 2 exactly backwards.

TEST(CapEventSemantics, StatusZeroWithTorqueIsSuccessfulCap) {
    EXPECT_TRUE(mas::is_successful_cap(0.0, 1.998));
    EXPECT_FALSE(mas::is_no_load(0.0, 1.998));
}

TEST(CapEventSemantics, StatusTwoWithZeroTorqueIsNoLoad) {
    EXPECT_TRUE(mas::is_no_load(2.0, 0.0));
    EXPECT_FALSE(mas::is_successful_cap(2.0, 0.0));
}

TEST(CapEventSemantics, FaultIsNeitherSuccessfulNorNoLoad) {
    EXPECT_TRUE(mas::is_fault_status(65.0));
    EXPECT_FALSE(mas::is_successful_cap(65.0, 1.997));
    EXPECT_FALSE(mas::is_no_load(65.0, 1.997));
}

TEST(CapEventSemantics, TransitionArtifactsAreNeither) {
    // 137 closures/day carry status 0 with zero torque; 155 carry status 2 with
    // torque. Both are transition artifacts and must not be counted either way.
    EXPECT_FALSE(mas::is_successful_cap(0.0, 0.0));
    EXPECT_FALSE(mas::is_no_load(0.0, 0.0));
    EXPECT_FALSE(mas::is_successful_cap(2.0, 1.998));
    EXPECT_FALSE(mas::is_no_load(2.0, 1.998));
}
```

- [ ] **Step 2: Add the test file to the build**

In `CMakeLists.txt`, find the `add_executable(unit_tests` list and add `tests/test_cap_event.cpp` alongside the other test sources.

- [ ] **Step 3: Run the test to verify it fails**

Run: `cmake --build build -j8 2>&1 | tail -5`
Expected: FAIL — `error: no member named 'is_successful_cap' in namespace 'mas'`

- [ ] **Step 4: Write the implementation**

In `core/include/mas/domain/CapEvent.hpp`, replace lines 9–13 (the comment and `is_fault_status`) with:

```cpp
// AROL Equatorque status codes, measured at closure over 2026-02-01 (spec §3.1).
// The joint (status, torque) distribution of 765,711 closures separates cleanly:
//
//   status 0,  torque > 0   -> 427,643/day  real capping operation, with load
//   status 2,  torque == 0  -> 337,772/day  "No Load" cycle: the counter advances
//                                           but no cap is applied (the idle signal)
//   status 65, torque > 0   ->       4/day  fault
//
// An earlier version of this comment had 0 and 2 backwards. It cannot be right:
// status 0 carries ~2.0 Nm on 427k closures a day, so it is not "idle", and
// status 2 carries zero torque on 337k, so it is not an "OK cap".
inline bool is_fault_status(double status) {
    return status == 65.0;
}

// A capping operation is a closure WITH load. No-load cycles are excluded from
// every success denominator (spec §3.2).
inline bool is_successful_cap(double status, double torque) {
    return status == 0.0 && torque > 0.0;
}

inline bool is_no_load(double status, double torque) {
    return status == 2.0 && torque == 0.0;
}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cmake --build build -j8 && cd build && ctest --output-on-failure 2>&1 | tail -3`
Expected: PASS — `100% tests passed, 0 tests failed out of 72`

- [ ] **Step 6: Commit**

```bash
git add core/include/mas/domain/CapEvent.hpp tests/test_cap_event.cpp CMakeLists.txt
git commit -m "fix(domain): status 0 is a real cap, status 2 is No-Load — the header had it backwards

Measured at closure over 2026-02-01 (765,711 closures): status 0 carries ~2.0 Nm
on 427,643 of them, and status 2 carries zero torque on 337,772. The header
comment claimed the opposite (0 = idle/held, 2 = OK cap), which is how a future
reader builds a wrong KPI. is_fault_status(65) was already correct.

No behavior change: the extractor stores status and torque faithfully and always
did. Adds is_successful_cap() and is_no_load() so the semantics have one home
instead of being re-derived per query. The No-Load predicate is also the idle
signal WP2 asks us to detect.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Python package scaffolding and config

Config-driven datasets are an explicit evaluation criterion (WP5). Nothing downstream may hard-code a path, a torque band, or a head count.

**Files:**
- Create: `python/requirements.txt`, `python/analytics/__init__.py`, `python/analytics/config.py`, `python/tests/test_config.py`

**Interfaces:**
- Produces: `Config` dataclass with fields `store_path: str`, `machine_id: str`, `torque_min: float`, `torque_max: float`, `mad_k: float`, `idle_min_seconds: int`, `success_status: float`, `fault_status: float`, `no_load_status: float`; and `load_config(path: str | None) -> Config`. Every tool takes a `Config` as its first argument.

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_config.py`:

```python
import json
import pytest
from analytics.config import Config, load_config, ConfigError


def test_defaults_are_the_measured_semantics():
    cfg = load_config(None)
    assert cfg.success_status == 0.0
    assert cfg.no_load_status == 2.0
    assert cfg.fault_status == 65.0


def test_load_from_file_overrides_defaults(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"store_path": "/data/x.duckdb", "torque_max": 3.0}))
    cfg = load_config(str(p))
    assert cfg.store_path == "/data/x.duckdb"
    assert cfg.torque_max == 3.0
    assert cfg.torque_min == 1.5   # untouched default


def test_torque_band_that_excludes_all_data_fails_loudly(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"torque_min": 5.0, "torque_max": 1.0}))
    with pytest.raises(ConfigError, match="torque_min .* torque_max"):
        load_config(str(p))


def test_unknown_key_fails_loudly(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"torqu_max": 3.0}))     # typo
    with pytest.raises(ConfigError, match="unknown config key"):
        load_config(str(p))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd python && python3 -m pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'analytics'`

- [ ] **Step 3: Write the implementation**

Create `python/requirements.txt`:

```
duckdb>=1.0
pandas>=2.0
numpy>=1.24
matplotlib>=3.7
pytest>=7.0
```

Install: `pip install -r python/requirements.txt`

Create `python/analytics/__init__.py`:

```python
"""Deterministic analytics toolkit over the cleaned cap_events store (spec WP2).

Every tool is a pure function: parameterised SQL in, a typed ToolResult out.
No tool computes a number the store cannot justify, and no tool raises for
degenerate data -- it returns a result whose status says so. That contract is
what lets Plan 7's report agent call these tools without ever being able to
invent a statistic.
"""
```

Create `python/analytics/config.py`:

```python
"""Configuration. No path, band, or threshold is hard-coded anywhere else.

WP5 requires configuration-driven datasets. This is also where the status
semantics live (spec §3.2): they were inferred from the measured joint
(status, torque) distribution, so if AROL confirms a different encoding it is a
change here and nowhere else.
"""
import json
from dataclasses import dataclass, fields


class ConfigError(Exception):
    """Bad config. Raised at startup, never mid-analysis."""


@dataclass(frozen=True)
class Config:
    store_path: str = "events.duckdb"
    machine_id: str = "MCC"

    # Status semantics, measured at closure (spec §3.1).
    success_status: float = 0.0    # + torque > 0  -> a real cap
    no_load_status: float = 2.0    # + torque == 0 -> No-Load cycle
    fault_status: float = 65.0

    # Expected torque operating band (Nm). Measured range: 1.885 - 2.317.
    torque_min: float = 1.5
    torque_max: float = 2.5

    # Robust deviation band: median +/- mad_k * MAD.
    mad_k: float = 3.0

    # A head is idle after this many seconds of sustained No-Load.
    idle_min_seconds: int = 300


def load_config(path):
    """Load config from a JSON file. `path=None` yields the defaults."""
    if path is None:
        return Config()
    with open(path) as fh:
        raw = json.load(fh)

    known = {f.name for f in fields(Config)}
    for key in raw:
        if key not in known:
            raise ConfigError(f"unknown config key {key!r}; known keys: {sorted(known)}")

    cfg = Config(**raw)
    if cfg.torque_min >= cfg.torque_max:
        raise ConfigError(
            f"torque_min ({cfg.torque_min}) must be < torque_max ({cfg.torque_max}); "
            "this band would exclude all data"
        )
    if cfg.idle_min_seconds <= 0:
        raise ConfigError(f"idle_min_seconds must be > 0, got {cfg.idle_min_seconds}")
    return cfg
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd python && python3 -m pytest tests/test_config.py -v`
Expected: PASS — 4 passed

- [ ] **Step 5: Commit**

```bash
git add python/requirements.txt python/analytics/ python/tests/test_config.py
git commit -m "feat(analytics): config module — status semantics and bands in one place

WP5 requires configuration-driven datasets, so no path, torque band, or
threshold may be hard-coded downstream. The status semantics live here too:
they are inferred from the measured joint (status, torque) distribution, so a
correction from AROL is a config change rather than a code change.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Result types and read-only store access

The `ToolResult` contract is what Plan 7's agent consumes. Getting it right here means the agent cannot drift from the toolkit.

**Files:**
- Create: `python/analytics/result.py`, `python/analytics/store.py`, `python/tests/conftest.py`, `python/tests/test_store.py`

**Interfaces:**
- Produces:
  - `Provenance(period, rows_scanned, filters, assumptions)` — dataclass
  - `ToolResult(tool, status, values, provenance, message)` — `status` is one of `"ok" | "insufficient_data" | "error"`; `values` is a `dict` or list of dicts; helpers `ToolResult.ok(...)`, `ToolResult.insufficient(...)`, `ToolResult.error(...)`
  - `connect(cfg) -> duckdb connection` (READ_ONLY)
  - `discover_heads(con) -> list[int]` — never assumes 36
  - `period_clause(period) -> (sql_fragment, params)` — accepts `"2026-02"` or `"2026-02..2026-04"` or `None` (all data)

- [ ] **Step 1: Write the failing test**

Create `python/tests/conftest.py`:

```python
"""A tiny hand-built store with known values, so every expectation is checkable by eye.

4 heads (NOT 36 -- head count must be discovered, spec §3.5).
Head 1: 3 successful caps (status 0, torque > 0)
Head 2: 1 successful, 1 fault (status 65)
Head 3: 2 no-load cycles (status 2, torque 0) -- never a capping operation
Head 4: nothing at all -- a head that never fires
"""
import duckdb
import pytest

from analytics.config import Config

ROWS = [
    # machine_id, head_id, ts,                     cap_seq, torque, status
    ("MCC", 1, "2026-02-01 00:00:00", 1, 2.00, 0.0),
    ("MCC", 1, "2026-02-01 00:00:10", 2, 2.10, 0.0),
    ("MCC", 1, "2026-02-01 00:00:20", 3, 1.90, 0.0),
    ("MCC", 2, "2026-02-01 00:00:00", 1, 2.00, 0.0),
    ("MCC", 2, "2026-02-01 00:00:30", 2, 1.99, 65.0),   # fault
    ("MCC", 3, "2026-02-01 00:00:00", 1, 0.00, 2.0),    # no-load
    ("MCC", 3, "2026-02-01 00:00:01", 2, 0.00, 2.0),    # no-load
]


@pytest.fixture
def tiny_store(tmp_path):
    path = tmp_path / "tiny.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE cap_events (
            machine_id VARCHAR NOT NULL,
            head_id    SMALLINT NOT NULL,
            ts         TIMESTAMP,
            cap_seq    BIGINT NOT NULL,
            app_torque REAL,
            status     REAL,
            delta      INTEGER,
            is_fault   BOOLEAN,
            aggregated BOOLEAN,
            is_reset   BOOLEAN,
            UNIQUE (machine_id, head_id, cap_seq)
        )
    """)
    for m, h, ts, seq, tq, st in ROWS:
        con.execute(
            "INSERT INTO cap_events VALUES (?,?,?,?,?,?,1,?,false,false)",
            [m, h, ts, seq, tq, st, st == 65.0],
        )
    # head 4 exists as a machine head but emitted nothing -- it is absent from the table
    con.close()
    return str(path)


@pytest.fixture
def tiny_cfg(tiny_store):
    return Config(store_path=tiny_store, machine_id="MCC")
```

Create `python/tests/test_store.py`:

```python
import pytest
from analytics.store import connect, discover_heads, period_clause
from analytics.result import ToolResult


def test_discovers_heads_from_data_not_a_hard_coded_36(tiny_cfg):
    con = connect(tiny_cfg)
    assert discover_heads(con) == [1, 2, 3]     # NOT range(1, 37)


def test_store_is_read_only(tiny_cfg):
    con = connect(tiny_cfg)
    with pytest.raises(Exception):
        con.execute("DELETE FROM cap_events")


def test_period_clause_single_month():
    sql, params = period_clause("2026-02")
    assert "ts >= ?" in sql and "ts < ?" in sql
    assert params == ["2026-02-01", "2026-03-01"]


def test_period_clause_range():
    sql, params = period_clause("2026-02..2026-04")
    assert params == ["2026-02-01", "2026-05-01"]


def test_period_clause_none_matches_everything():
    sql, params = period_clause(None)
    assert sql == "TRUE"
    assert params == []


def test_period_clause_rejects_garbage():
    with pytest.raises(ValueError, match="unparseable period"):
        period_clause("last tuesday")


def test_result_helpers_carry_status():
    r = ToolResult.insufficient("overview", "no rows in period", period="2026-09")
    assert r.status == "insufficient_data"
    assert r.values == {}
    assert r.provenance.period == "2026-09"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd python && python3 -m pytest tests/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'analytics.store'`

- [ ] **Step 3: Write the implementation**

Create `python/analytics/result.py`:

```python
"""The tool contract. Plan 7's agent consumes exactly this and nothing else.

Two rules make hallucinated statistics structurally impossible downstream:
  1. Every number a tool returns is accompanied by the provenance that justifies
     it -- the period, the rows scanned, the filters applied.
  2. Degenerate data is a *value*, not an exception: a tool returns status
     "insufficient_data" and the report names the gap in confidence/limits,
     rather than the pipeline dying or silently emitting a fabricated 0%.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Provenance:
    period: str | None = None
    rows_scanned: int = 0
    filters: list = field(default_factory=list)
    assumptions: list = field(default_factory=list)


@dataclass(frozen=True)
class ToolResult:
    tool: str
    status: str                 # "ok" | "insufficient_data" | "error"
    values: object              # dict, or list of dicts
    provenance: Provenance
    message: str = ""

    @staticmethod
    def ok(tool, values, *, period=None, rows_scanned=0, filters=None, assumptions=None):
        return ToolResult(
            tool=tool, status="ok", values=values,
            provenance=Provenance(period, rows_scanned, filters or [], assumptions or []),
        )

    @staticmethod
    def insufficient(tool, message, *, period=None, rows_scanned=0):
        return ToolResult(
            tool=tool, status="insufficient_data", values={},
            provenance=Provenance(period, rows_scanned), message=message,
        )

    @staticmethod
    def error(tool, message, *, period=None):
        return ToolResult(
            tool=tool, status="error", values={},
            provenance=Provenance(period), message=message,
        )
```

Create `python/analytics/store.py`:

```python
"""Read-only access to the cleaned cap_events store.

The toolkit never writes to the event store -- the C++ MAS owns it. Head count
is DISCOVERED, never assumed: our machine has 36 heads but the brief's example
dataset has 48, and WP5 requires the solution to generalise (spec §3.5).
"""
import duckdb


def connect(cfg):
    return duckdb.connect(cfg.store_path, read_only=True)


def discover_heads(con):
    """The heads actually present in the data. Never range(1, 37)."""
    rows = con.execute(
        "SELECT DISTINCT head_id FROM cap_events ORDER BY head_id"
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd python && python3 -m pytest tests/ -v`
Expected: PASS — 11 passed

- [ ] **Step 5: Commit**

```bash
git add python/analytics/result.py python/analytics/store.py python/tests/
git commit -m "feat(analytics): ToolResult contract and read-only store access

ToolResult is the interface Plan 7's agent consumes. Two properties make
hallucinated statistics structurally impossible: every value carries the
provenance justifying it, and degenerate data is a status ('insufficient_data')
rather than an exception, so a report names the gap instead of dying or
fabricating a zero.

Head count is discovered from the data, never assumed to be 36 — the brief's
example dataset has 48 heads and WP5 requires the solution to generalise.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: `overview()` — counts, time range, and the WP1 validation checks

Answers the brief's exploration queries ("how many capping operations?", "time range covered?", "any missing or invalid torque values?") and carries the WP1 validation checks the spec moved here.

**Files:**
- Create: `python/analytics/tools/__init__.py`, `python/analytics/tools/overview.py`, `python/tests/test_overview.py`

**Interfaces:**
- Consumes: `Config`, `connect`, `period_clause`, `ToolResult` (Tasks 2–3)
- Produces: `overview(cfg, period=None) -> ToolResult` with `values` keys: `capping_operations`, `successful`, `failed`, `no_load_cycles`, `heads`, `ts_min`, `ts_max`, `invalid_torque`, `null_torque`, `counter_resets`

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_overview.py`:

```python
from analytics.tools.overview import overview


def test_counts_separate_real_caps_from_no_load(tiny_cfg):
    r = overview(tiny_cfg, period="2026-02")
    assert r.status == "ok"
    v = r.values
    # Heads 1 and 2 produced 5 closures with torque; head 3's 2 are no-load.
    assert v["capping_operations"] == 5      # NOT 7 — no-load is not a capping op
    assert v["successful"] == 4
    assert v["failed"] == 1
    assert v["no_load_cycles"] == 2
    assert v["heads"] == [1, 2, 3]


def test_reports_time_range(tiny_cfg):
    r = overview(tiny_cfg, period="2026-02")
    assert str(r.values["ts_min"]) == "2026-02-01 00:00:00"
    assert str(r.values["ts_max"]) == "2026-02-01 00:00:30"


def test_empty_period_is_insufficient_not_a_crash(tiny_cfg):
    r = overview(tiny_cfg, period="2026-09")
    assert r.status == "insufficient_data"
    assert "no capping events" in r.message
    assert r.values == {}


def test_provenance_records_rows_and_assumptions(tiny_cfg):
    r = overview(tiny_cfg, period="2026-02")
    assert r.provenance.rows_scanned == 7          # all closures, incl. no-load
    assert r.provenance.period == "2026-02"
    assert any("no-load" in a for a in r.provenance.assumptions)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd python && python3 -m pytest tests/test_overview.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'analytics.tools'`

- [ ] **Step 3: Write the implementation**

Create `python/analytics/tools/__init__.py`:

```python
"""The seven WP2 tools. Each is a pure function taking Config, returning ToolResult."""
```

Create `python/analytics/tools/overview.py`:

```python
"""Dataset exploration + WP1 validation checks.

Answers the brief's exploration queries: how many capping operations, what time
range, any missing or invalid torque. The headline distinction it enforces
everywhere: a *capping operation* is a closure WITH load. No-load cycles (the
counter advances, no cap is applied) are counted separately and never inflate a
success denominator (spec §3.2).
"""
from analytics.result import ToolResult
from analytics.store import connect, discover_heads, period_clause

ASSUMPTION = (
    "a capping operation is a closure with torque > 0; no-load cycles "
    "(status 2, torque 0) are excluded from success denominators"
)


def overview(cfg, period=None):
    con = connect(cfg)
    where, params = period_clause(period)

    row = con.execute(f"""
        SELECT
            COUNT(*)                                                    AS closures,
            COUNT(*) FILTER (WHERE app_torque > 0)                      AS capping_operations,
            COUNT(*) FILTER (WHERE status = ? AND app_torque > 0)       AS successful,
            COUNT(*) FILTER (WHERE status = ?)                          AS failed,
            COUNT(*) FILTER (WHERE status = ? AND app_torque = 0)       AS no_load,
            MIN(ts)                                                     AS ts_min,
            MAX(ts)                                                     AS ts_max,
            COUNT(*) FILTER (WHERE app_torque IS NULL)                  AS null_torque,
            COUNT(*) FILTER (WHERE app_torque > 0
                             AND (app_torque < ? OR app_torque > ?))    AS invalid_torque,
            COUNT(*) FILTER (WHERE is_reset)                            AS counter_resets
        FROM cap_events
        WHERE {where}
    """, [cfg.success_status, cfg.fault_status, cfg.no_load_status,
          cfg.torque_min, cfg.torque_max] + params).fetchone()

    closures = row[0]
    if closures == 0:
        return ToolResult.insufficient(
            "overview", f"no capping events in period {period!r}", period=period
        )

    heads = con.execute(
        f"SELECT DISTINCT head_id FROM cap_events WHERE {where} ORDER BY head_id", params
    ).fetchall()

    return ToolResult.ok(
        "overview",
        {
            "capping_operations": row[1],
            "successful": row[2],
            "failed": row[3],
            "no_load_cycles": row[4],
            "heads": [int(h[0]) for h in heads],
            "ts_min": row[5],
            "ts_max": row[6],
            "null_torque": row[7],
            "invalid_torque": row[8],
            "counter_resets": row[9],
        },
        period=period,
        rows_scanned=closures,
        filters=[f"period={period}"] if period else [],
        assumptions=[ASSUMPTION],
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd python && python3 -m pytest tests/ -v`
Expected: PASS — 15 passed

- [ ] **Step 5: Commit**

```bash
git add python/analytics/tools/ python/tests/test_overview.py
git commit -m "feat(analytics): overview() — counts, time range, validation checks

Answers the brief's data-exploration queries and carries the WP1 validation
checks (null torque, torque outside the configured band, counter resets).

Enforces the distinction the whole toolkit rests on: a capping operation is a
closure WITH load. The tiny-store test pins it — 7 closures, but only 5 capping
operations, because head 3's two no-load cycles are not capping operations and
must never inflate a success denominator.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: `success_rates()`

The brief's flagship query — "what is the success rate per capping head?" — and its expected-output table (slide 17).

**Files:**
- Create: `python/analytics/tools/success.py`, `python/tests/test_success.py`

**Interfaces:**
- Consumes: Tasks 2–3
- Produces: `success_rates(cfg, period=None, by="head") -> ToolResult`; `by` ∈ `{"head", "day", "overall"}`. For `by="head"`, `values` is a list of dicts with keys `head_id`, `total`, `successful`, `failed`, `success_rate`. For `by="overall"`, a single dict with `total`, `successful`, `failed`, `success_rate`, `lowest_head`.

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_success.py`:

```python
import pytest
from analytics.tools.success import success_rates


def test_success_rate_per_head_excludes_no_load(tiny_cfg):
    r = success_rates(tiny_cfg, period="2026-02", by="head")
    assert r.status == "ok"
    by_head = {v["head_id"]: v for v in r.values}

    # Head 1: 3 successful, 0 failed -> 100%
    assert by_head[1]["total"] == 3
    assert by_head[1]["success_rate"] == pytest.approx(1.0)

    # Head 2: 1 successful, 1 fault -> 50%
    assert by_head[2]["total"] == 2
    assert by_head[2]["failed"] == 1
    assert by_head[2]["success_rate"] == pytest.approx(0.5)

    # Head 3 did only no-load cycles: it performed ZERO capping operations, so it
    # must NOT appear with a fabricated 0% success rate.
    assert 3 not in by_head


def test_overall_identifies_the_lowest_head(tiny_cfg):
    r = success_rates(tiny_cfg, period="2026-02", by="overall")
    v = r.values
    assert v["total"] == 5
    assert v["successful"] == 4
    assert v["success_rate"] == pytest.approx(0.8)
    assert v["lowest_head"] == 2


def test_by_day(tiny_cfg):
    r = success_rates(tiny_cfg, period="2026-02", by="day")
    assert len(r.values) == 1
    assert str(r.values[0]["day"]) == "2026-02-01"
    assert r.values[0]["successful"] == 4


def test_empty_period_is_insufficient(tiny_cfg):
    r = success_rates(tiny_cfg, period="2026-09", by="head")
    assert r.status == "insufficient_data"


def test_rejects_unknown_grouping(tiny_cfg):
    r = success_rates(tiny_cfg, period="2026-02", by="wombat")
    assert r.status == "error"
    assert "by must be" in r.message
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd python && python3 -m pytest tests/test_success.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'analytics.tools.success'`

- [ ] **Step 3: Write the implementation**

Create `python/analytics/tools/success.py`:

```python
"""Success rates — the brief's flagship KPI (slides 16-17).

A head that only ever cycled with no load has performed zero capping operations.
It is omitted rather than reported at 0%: a fabricated zero would read as a
catastrophically failing head when in truth nothing was ever capped.
"""
from analytics.result import ToolResult
from analytics.store import connect, period_clause
from analytics.tools.overview import ASSUMPTION

_GROUPS = {"head": "head_id", "day": "CAST(ts AS DATE)", "overall": None}


def success_rates(cfg, period=None, by="head"):
    if by not in _GROUPS:
        return ToolResult.error(
            "success_rates", f"by must be one of {sorted(_GROUPS)}, got {by!r}", period=period
        )

    con = connect(cfg)
    where, params = period_clause(period)
    sem = [cfg.success_status, cfg.fault_status]

    # Only closures WITH load are capping operations (spec §3.2).
    select = """
        COUNT(*)                                  AS total,
        COUNT(*) FILTER (WHERE status = ?)        AS successful,
        COUNT(*) FILTER (WHERE status = ?)        AS failed
    """
    base = f"FROM cap_events WHERE app_torque > 0 AND {where}"

    if by == "overall":
        row = con.execute(f"SELECT {select} {base}", sem + params).fetchone()
        if row[0] == 0:
            return ToolResult.insufficient(
                "success_rates", f"no capping operations in period {period!r}", period=period
            )
        per_head = con.execute(
            f"""SELECT head_id, {select} {base} GROUP BY head_id
                HAVING COUNT(*) > 0 ORDER BY 4 DESC, head_id""",
            sem + params,
        ).fetchall()
        lowest = min(per_head, key=lambda h: (h[2] / h[1], h[0]))[0] if per_head else None
        values = {
            "total": row[0],
            "successful": row[1],
            "failed": row[2],
            "success_rate": row[1] / row[0],
            "lowest_head": int(lowest) if lowest is not None else None,
        }
    else:
        group = _GROUPS[by]
        label = "head_id" if by == "head" else "day"
        rows = con.execute(
            f"""SELECT {group} AS {label}, {select} {base}
                GROUP BY 1 ORDER BY 1""",
            sem + params,
        ).fetchall()
        if not rows:
            return ToolResult.insufficient(
                "success_rates", f"no capping operations in period {period!r}", period=period
            )
        values = [
            {
                label: int(r[0]) if by == "head" else r[0],
                "total": r[1],
                "successful": r[2],
                "failed": r[3],
                "success_rate": r[2] / r[1],
            }
            for r in rows
        ]

    scanned = sum(v["total"] for v in values) if isinstance(values, list) else values["total"]
    return ToolResult.ok(
        "success_rates", values,
        period=period, rows_scanned=scanned,
        filters=["app_torque > 0 (capping operations only)"],
        assumptions=[ASSUMPTION],
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd python && python3 -m pytest tests/ -v`
Expected: PASS — 20 passed

- [ ] **Step 5: Commit**

```bash
git add python/analytics/tools/success.py python/tests/test_success.py
git commit -m "feat(analytics): success_rates() by head, day, or overall

The brief's flagship KPI. A head that only ever cycled with no load performed
zero capping operations and is omitted rather than reported at 0% — a fabricated
zero would read as a catastrophically failing head when in truth nothing was
ever capped.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: `torque_stats()`

Slide 18's expected output: mean/min/max/stddev of closing torque for successful closures, plus per-head variability.

**Files:**
- Create: `python/analytics/tools/torque.py`, `python/tests/test_torque.py`

**Interfaces:**
- Consumes: Tasks 2–3
- Produces: `torque_stats(cfg, period=None, outcome="successful", by=None) -> ToolResult`; `outcome` ∈ `{"successful", "failed", "all"}`; `by=None` → single dict with `n`, `mean`, `min`, `max`, `stddev`, `median`; `by="head"` → list of dicts adding `head_id` and sorted by `stddev` descending (answers "which head shows the highest torque variability?").

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_torque.py`:

```python
import pytest
from analytics.tools.torque import torque_stats


def test_stats_over_successful_closures_only(tiny_cfg):
    r = torque_stats(tiny_cfg, period="2026-02", outcome="successful")
    v = r.values
    # Successful torques: 2.00, 2.10, 1.90 (head 1) and 2.00 (head 2) = 4 values.
    # The fault (1.99) and the two no-load zeros are excluded.
    assert v["n"] == 4
    assert v["mean"] == pytest.approx(2.0)
    assert v["min"] == pytest.approx(1.90)
    assert v["max"] == pytest.approx(2.10)


def test_zero_torque_never_dilutes_the_mean(tiny_cfg):
    """The whole point of the semantics fix: 337k no-load zeros would drag the
    mean toward zero if they were treated as capping operations."""
    r = torque_stats(tiny_cfg, period="2026-02", outcome="all")
    assert r.values["n"] == 5          # 5 closures with load, NOT 7
    assert r.values["mean"] > 1.9      # nowhere near 0


def test_per_head_variability_ranking(tiny_cfg):
    r = torque_stats(tiny_cfg, period="2026-02", outcome="successful", by="head")
    # Head 1 (2.00, 2.10, 1.90) varies; head 2 (single value 2.00) does not.
    assert r.values[0]["head_id"] == 1
    assert r.values[0]["stddev"] > 0


def test_empty_period_is_insufficient(tiny_cfg):
    r = torque_stats(tiny_cfg, period="2026-09")
    assert r.status == "insufficient_data"


def test_rejects_unknown_outcome(tiny_cfg):
    r = torque_stats(tiny_cfg, period="2026-02", outcome="sideways")
    assert r.status == "error"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd python && python3 -m pytest tests/test_torque.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'analytics.tools.torque'`

- [ ] **Step 3: Write the implementation**

Create `python/analytics/tools/torque.py`:

```python
"""Torque statistics (brief slide 18).

Always filtered to closures WITH load. This is not a detail: 337,772 no-load
cycles a day carry torque 0.0, and including them would drag every mean toward
zero and invent a bimodal distribution that does not exist.
"""
from analytics.result import ToolResult
from analytics.store import connect, period_clause
from analytics.tools.overview import ASSUMPTION

_OUTCOMES = ("successful", "failed", "all")


def torque_stats(cfg, period=None, outcome="successful", by=None):
    if outcome not in _OUTCOMES:
        return ToolResult.error(
            "torque_stats", f"outcome must be one of {list(_OUTCOMES)}, got {outcome!r}",
            period=period,
        )
    if by not in (None, "head"):
        return ToolResult.error("torque_stats", f"by must be None or 'head', got {by!r}",
                                period=period)

    con = connect(cfg)
    where, params = period_clause(period)

    cond, sem = "app_torque > 0", []
    if outcome == "successful":
        cond, sem = "app_torque > 0 AND status = ?", [cfg.success_status]
    elif outcome == "failed":
        cond, sem = "status = ?", [cfg.fault_status]

    agg = """
        COUNT(*)          AS n,
        AVG(app_torque)   AS mean,
        MIN(app_torque)   AS min,
        MAX(app_torque)   AS max,
        STDDEV_SAMP(app_torque) AS stddev,
        MEDIAN(app_torque)      AS median
    """
    base = f"FROM cap_events WHERE {cond} AND {where}"

    if by == "head":
        rows = con.execute(
            f"SELECT head_id, {agg} {base} GROUP BY head_id ORDER BY stddev DESC NULLS LAST, head_id",
            sem + params,
        ).fetchall()
        if not rows:
            return ToolResult.insufficient(
                "torque_stats", f"no {outcome} closures in period {period!r}", period=period
            )
        values = [
            {"head_id": int(r[0]), "n": r[1], "mean": r[2], "min": r[3],
             "max": r[4], "stddev": r[5] or 0.0, "median": r[6]}
            for r in rows
        ]
        scanned = sum(v["n"] for v in values)
    else:
        r = con.execute(f"SELECT {agg} {base}", sem + params).fetchone()
        if r[0] == 0:
            return ToolResult.insufficient(
                "torque_stats", f"no {outcome} closures in period {period!r}", period=period
            )
        values = {"n": r[0], "mean": r[1], "min": r[2], "max": r[3],
                  "stddev": r[4] or 0.0, "median": r[5]}
        scanned = r[0]

    return ToolResult.ok(
        "torque_stats", values,
        period=period, rows_scanned=scanned,
        filters=[f"outcome={outcome}", "app_torque > 0 (no-load excluded)"],
        assumptions=[ASSUMPTION],
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd python && python3 -m pytest tests/ -v`
Expected: PASS — 25 passed

- [ ] **Step 5: Commit**

```bash
git add python/analytics/tools/torque.py python/tests/test_torque.py
git commit -m "feat(analytics): torque_stats() with per-head variability ranking

Always filtered to closures with load. A regression test pins why: 337,772
no-load cycles a day carry torque 0.0, and counting them would drag every mean
toward zero and invent a bimodal distribution that does not exist.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: `capping_speed()` — pieces/hour

The brief's WP1 requirement, computed in SQL (see the deviation note above).

**Files:**
- Create: `python/analytics/tools/speed.py`, `python/tests/test_speed.py`

**Interfaces:**
- Consumes: Tasks 2–3
- Produces: `capping_speed(cfg, period=None, bucket="hour") -> ToolResult`; `bucket` ∈ `{"hour", "day"}`; `values` is a list of dicts with `bucket_start`, `caps`, `pieces_per_hour`, plus a summary dict at `values[-1]` is NOT used — instead `ToolResult.values` is `{"buckets": [...], "mean_pieces_per_hour": float}`.

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_speed.py`:

```python
import pytest
from analytics.tools.speed import capping_speed


def test_pieces_per_hour_counts_only_real_caps(tiny_cfg):
    r = capping_speed(tiny_cfg, period="2026-02", bucket="hour")
    assert r.status == "ok"
    buckets = r.values["buckets"]
    assert len(buckets) == 1
    # 5 closures with load in the 00:00 hour (no-load cycles do not produce pieces).
    assert buckets[0]["caps"] == 5
    assert buckets[0]["pieces_per_hour"] == pytest.approx(5.0)


def test_day_bucket_normalises_to_an_hourly_rate(tiny_cfg):
    r = capping_speed(tiny_cfg, period="2026-02", bucket="day")
    b = r.values["buckets"][0]
    assert b["caps"] == 5
    assert b["pieces_per_hour"] == pytest.approx(5 / 24)


def test_empty_period_is_insufficient(tiny_cfg):
    r = capping_speed(tiny_cfg, period="2026-09")
    assert r.status == "insufficient_data"


def test_rejects_unknown_bucket(tiny_cfg):
    r = capping_speed(tiny_cfg, period="2026-02", bucket="fortnight")
    assert r.status == "error"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd python && python3 -m pytest tests/test_speed.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'analytics.tools.speed'`

- [ ] **Step 3: Write the implementation**

Create `python/analytics/tools/speed.py`:

```python
"""Capping speed in pieces/hour (brief WP1).

The brief describes this as an "incremental average" computed during ingestion.
We compute it in SQL over the persisted events instead: it is the same number --
caps per unit time -- and needs no schema column, no migration, and no
reprocessing of the 89 day-files. Only closures WITH load produce a piece.
"""
from analytics.result import ToolResult
from analytics.store import connect, period_clause
from analytics.tools.overview import ASSUMPTION

_BUCKETS = {"hour": ("HOUR", 1.0), "day": ("DAY", 24.0)}


def capping_speed(cfg, period=None, bucket="hour"):
    if bucket not in _BUCKETS:
        return ToolResult.error(
            "capping_speed", f"bucket must be one of {sorted(_BUCKETS)}, got {bucket!r}",
            period=period,
        )
    unit, hours_per_bucket = _BUCKETS[bucket]

    con = connect(cfg)
    where, params = period_clause(period)

    rows = con.execute(f"""
        SELECT DATE_TRUNC('{unit}', ts) AS bucket_start, COUNT(*) AS caps
        FROM cap_events
        WHERE app_torque > 0 AND {where}
        GROUP BY 1 ORDER BY 1
    """, params).fetchall()

    if not rows:
        return ToolResult.insufficient(
            "capping_speed", f"no capping operations in period {period!r}", period=period
        )

    buckets = [
        {"bucket_start": r[0], "caps": r[1], "pieces_per_hour": r[1] / hours_per_bucket}
        for r in rows
    ]
    total = sum(b["caps"] for b in buckets)
    return ToolResult.ok(
        "capping_speed",
        {
            "buckets": buckets,
            "mean_pieces_per_hour": sum(b["pieces_per_hour"] for b in buckets) / len(buckets),
        },
        period=period, rows_scanned=total,
        filters=[f"bucket={bucket}", "app_torque > 0 (only real caps produce pieces)"],
        assumptions=[ASSUMPTION],
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd python && python3 -m pytest tests/ -v`
Expected: PASS — 29 passed

- [ ] **Step 5: Commit**

```bash
git add python/analytics/tools/speed.py python/tests/test_speed.py
git commit -m "feat(analytics): capping_speed() in pieces/hour

The brief puts this in ingestion as an incremental average; we compute it in SQL
over the persisted events. Same number, but no schema column, no migration, and
no reprocessing of the 89 day-files. Only closures with load produce a piece —
a no-load cycle advances the counter without capping anything.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: `idle_periods()` — sustained No-Load detection

The brief: *"Identify machine idle state based on 'No Load' for a sustained period for every head."* The semantics fix in Task 1 is what makes this possible.

**Files:**
- Create: `python/analytics/tools/idle.py`, `python/tests/test_idle.py`

**Interfaces:**
- Consumes: Tasks 2–3
- Produces: `idle_periods(cfg, period=None, min_seconds=None) -> ToolResult`; `min_seconds` defaults to `cfg.idle_min_seconds`. `values` = `{"periods": [ {head_id, start, end, duration_seconds, cycles} ], "total_idle_seconds": int}`.

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_idle.py`:

```python
import duckdb
import pytest
from analytics.config import Config
from analytics.tools.idle import idle_periods


@pytest.fixture
def idle_store(tmp_path):
    """Head 1: a 60-second sustained no-load run, then a real cap.
       Head 2: two isolated no-load cycles, never sustained."""
    path = tmp_path / "idle.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE cap_events (
            machine_id VARCHAR NOT NULL, head_id SMALLINT NOT NULL, ts TIMESTAMP,
            cap_seq BIGINT NOT NULL, app_torque REAL, status REAL, delta INTEGER,
            is_fault BOOLEAN, aggregated BOOLEAN, is_reset BOOLEAN,
            UNIQUE (machine_id, head_id, cap_seq))
    """)
    rows = []
    for i in range(7):                                   # 0,10,...,60s -> 60s span
        rows.append(("MCC", 1, f"2026-02-01 00:00:{i*10:02d}", i + 1, 0.0, 2.0))
    rows.append(("MCC", 1, "2026-02-01 00:02:00", 100, 2.0, 0.0))   # real cap
    rows.append(("MCC", 2, "2026-02-01 00:00:00", 1, 0.0, 2.0))
    rows.append(("MCC", 2, "2026-02-01 00:00:05", 2, 2.0, 0.0))     # cap breaks the run
    rows.append(("MCC", 2, "2026-02-01 00:00:10", 3, 0.0, 2.0))
    for m, h, ts, seq, tq, st in rows:
        con.execute("INSERT INTO cap_events VALUES (?,?,?,?,?,?,1,false,false,false)",
                    [m, h, ts, seq, tq, st])
    con.close()
    return str(path)


def test_finds_the_sustained_run_and_ignores_isolated_cycles(idle_store):
    cfg = Config(store_path=idle_store, idle_min_seconds=30)
    r = idle_periods(cfg, period="2026-02")
    assert r.status == "ok"
    periods = r.values["periods"]
    assert len(periods) == 1                     # head 2's isolated cycles don't qualify
    p = periods[0]
    assert p["head_id"] == 1
    assert p["duration_seconds"] == 60
    assert p["cycles"] == 7


def test_threshold_above_the_run_finds_nothing(idle_store):
    cfg = Config(store_path=idle_store, idle_min_seconds=300)
    r = idle_periods(cfg, period="2026-02")
    assert r.status == "insufficient_data"
    assert "no idle periods" in r.message


def test_min_seconds_argument_overrides_config(idle_store):
    cfg = Config(store_path=idle_store, idle_min_seconds=300)
    r = idle_periods(cfg, period="2026-02", min_seconds=30)
    assert r.status == "ok"
    assert len(r.values["periods"]) == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd python && python3 -m pytest tests/test_idle.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'analytics.tools.idle'`

- [ ] **Step 3: Write the implementation**

Create `python/analytics/tools/idle.py`:

```python
"""Machine idle state: sustained No-Load runs, per head (brief WP2).

A No-Load cycle is status 2 with zero torque -- the head cycles and the counter
advances, but no cap is applied. A *run* of them, longer than a threshold, is the
machine idling. This tool only became possible once the status semantics were
corrected: the old reading called status 2 an "OK cap", which would have made
idle time invisible.

Runs are found with the classic gaps-and-islands trick: number the rows per head,
number the no-load rows per head, and the difference is constant within a run.
"""
from analytics.result import ToolResult
from analytics.store import connect, period_clause


def idle_periods(cfg, period=None, min_seconds=None):
    threshold = cfg.idle_min_seconds if min_seconds is None else min_seconds
    con = connect(cfg)
    where, params = period_clause(period)

    rows = con.execute(f"""
        WITH marked AS (
            SELECT head_id, ts,
                   (status = ? AND app_torque = 0) AS no_load,
                   ROW_NUMBER() OVER (PARTITION BY head_id ORDER BY ts) AS rn_all,
                   ROW_NUMBER() OVER (
                       PARTITION BY head_id, (status = ? AND app_torque = 0) ORDER BY ts
                   ) AS rn_grp
            FROM cap_events
            WHERE {where}
        ),
        runs AS (
            SELECT head_id, rn_all - rn_grp AS island, MIN(ts) AS start, MAX(ts) AS end,
                   COUNT(*) AS cycles
            FROM marked
            WHERE no_load
            GROUP BY head_id, island
        )
        SELECT head_id, start, end, cycles,
               CAST(DATE_DIFF('second', start, end) AS BIGINT) AS duration_seconds
        FROM runs
        WHERE DATE_DIFF('second', start, end) >= ?
        ORDER BY head_id, start
    """, [cfg.no_load_status, cfg.no_load_status] + params + [threshold]).fetchall()

    if not rows:
        return ToolResult.insufficient(
            "idle_periods",
            f"no idle periods of >= {threshold}s in period {period!r}",
            period=period,
        )

    periods = [
        {"head_id": int(r[0]), "start": r[1], "end": r[2],
         "cycles": r[3], "duration_seconds": int(r[4])}
        for r in rows
    ]
    return ToolResult.ok(
        "idle_periods",
        {"periods": periods,
         "total_idle_seconds": sum(p["duration_seconds"] for p in periods)},
        period=period,
        rows_scanned=sum(p["cycles"] for p in periods),
        filters=[f"min_seconds={threshold}"],
        assumptions=["an idle period is a sustained run of no-load cycles (status 2, torque 0)"],
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd python && python3 -m pytest tests/ -v`
Expected: PASS — 32 passed

- [ ] **Step 5: Commit**

```bash
git add python/analytics/tools/idle.py python/tests/test_idle.py
git commit -m "feat(analytics): idle_periods() — sustained No-Load detection

The brief asks to identify machine idle state from sustained 'No Load'. This
tool only became possible once the status semantics were corrected: the old
reading called status 2 an 'OK cap', which would have made idle time invisible.

Runs are found with gaps-and-islands over the per-head row numbering. An
isolated no-load cycle broken by a real cap does not qualify — the test pins it.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 9: `anomalies()` — threshold and robust deviation

Deterministic, per spec §5.3. No sklearn.

**Files:**
- Create: `python/analytics/tools/anomaly.py`, `python/tests/test_anomaly.py`

**Interfaces:**
- Consumes: Tasks 2–3
- Produces: `anomalies(cfg, period=None, method="both") -> ToolResult`; `method` ∈ `{"threshold", "deviation", "both"}`. `values` = `{"faults": [...], "threshold_hits": [...], "deviation_hits": [...], "counts": {...}}`. Each hit dict has `head_id`, `ts`, `app_torque`, `reason`.

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_anomaly.py`:

```python
import duckdb
import pytest
from analytics.config import Config
from analytics.tools.anomaly import anomalies


@pytest.fixture
def anomaly_store(tmp_path):
    """Head 1: 20 tight caps around 2.0, plus one at 2.9 (way outside the band).
       Head 2: one fault (status 65)."""
    path = tmp_path / "anom.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE cap_events (
            machine_id VARCHAR NOT NULL, head_id SMALLINT NOT NULL, ts TIMESTAMP,
            cap_seq BIGINT NOT NULL, app_torque REAL, status REAL, delta INTEGER,
            is_fault BOOLEAN, aggregated BOOLEAN, is_reset BOOLEAN,
            UNIQUE (machine_id, head_id, cap_seq))
    """)
    for i in range(20):
        tq = 2.00 + (0.01 if i % 2 else -0.01)
        con.execute("INSERT INTO cap_events VALUES ('MCC',1,?,?,?,0.0,1,false,false,false)",
                    [f"2026-02-01 00:00:{i:02d}", i + 1, tq])
    con.execute("INSERT INTO cap_events VALUES "
                "('MCC',1,'2026-02-01 00:01:00',100,2.9,0.0,1,false,false,false)")
    con.execute("INSERT INTO cap_events VALUES "
                "('MCC',2,'2026-02-01 00:02:00',1,1.99,65.0,1,true,false,false)")
    con.close()
    return str(path)


def test_threshold_flags_torque_outside_the_configured_band(anomaly_store):
    cfg = Config(store_path=anomaly_store, torque_min=1.5, torque_max=2.5)
    r = anomalies(cfg, period="2026-02", method="threshold")
    hits = r.values["threshold_hits"]
    assert len(hits) == 1
    assert hits[0]["app_torque"] == pytest.approx(2.9)
    assert "outside band" in hits[0]["reason"]


def test_deviation_flags_the_outlier_against_its_own_heads_band(anomaly_store):
    cfg = Config(store_path=anomaly_store, torque_min=0.0, torque_max=10.0, mad_k=3.0)
    r = anomalies(cfg, period="2026-02", method="deviation")
    hits = r.values["deviation_hits"]
    # 2.9 is far outside head 1's median +/- 3*MAD, even though it is inside the
    # wide-open threshold band — this is what makes the two methods complementary.
    assert [h["app_torque"] for h in hits] == pytest.approx([2.9])


def test_faults_are_always_reported(anomaly_store):
    cfg = Config(store_path=anomaly_store)
    r = anomalies(cfg, period="2026-02", method="both")
    assert len(r.values["faults"]) == 1
    assert r.values["faults"][0]["head_id"] == 2


def test_healthy_data_yields_zero_hits_not_an_error(tiny_cfg):
    r = anomalies(tiny_cfg, period="2026-02", method="both")
    assert r.status == "ok"
    assert r.values["counts"]["threshold_hits"] == 0


def test_rejects_unknown_method(tiny_cfg):
    r = anomalies(tiny_cfg, period="2026-02", method="vibes")
    assert r.status == "error"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd python && python3 -m pytest tests/test_anomaly.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'analytics.tools.anomaly'`

- [ ] **Step 3: Write the implementation**

Create `python/analytics/tools/anomaly.py`:

```python
"""Deterministic anomaly detection: thresholds + robust statistical deviation.

The brief asks for "simple anomaly detection (thresholds, statistical
deviations)" and requires WP2 to be deterministic so the agent has reliable
tools to call. No model, no training, no randomness -- the same data always
yields the same flags.

The deviation band is median +/- k*MAD rather than mean +/- k*sigma: a handful of
extreme outliers inflate sigma enough to hide themselves, and MAD does not have
that problem.

Note the two methods are complementary, not redundant: threshold catches torque
outside the *machine's* spec band; deviation catches a head drifting away from
*its own* normal, even while still inside the band.
"""
from analytics.result import ToolResult
from analytics.store import connect, period_clause

_METHODS = ("threshold", "deviation", "both")


def anomalies(cfg, period=None, method="both"):
    if method not in _METHODS:
        return ToolResult.error(
            "anomalies", f"method must be one of {list(_METHODS)}, got {method!r}",
            period=period,
        )

    con = connect(cfg)
    where, params = period_clause(period)

    faults = [
        {"head_id": int(r[0]), "ts": r[1], "app_torque": r[2], "reason": "fault status"}
        for r in con.execute(
            f"""SELECT head_id, ts, app_torque FROM cap_events
                WHERE status = ? AND {where} ORDER BY ts""",
            [cfg.fault_status] + params,
        ).fetchall()
    ]

    threshold_hits = []
    if method in ("threshold", "both"):
        threshold_hits = [
            {"head_id": int(r[0]), "ts": r[1], "app_torque": r[2],
             "reason": f"torque outside band [{cfg.torque_min}, {cfg.torque_max}]"}
            for r in con.execute(
                f"""SELECT head_id, ts, app_torque FROM cap_events
                    WHERE app_torque > 0 AND (app_torque < ? OR app_torque > ?)
                      AND {where}
                    ORDER BY ts""",
                [cfg.torque_min, cfg.torque_max] + params,
            ).fetchall()
        ]

    deviation_hits = []
    if method in ("deviation", "both"):
        # MEDIAN(|x - median|) per head, then flag |x - median| > k * MAD.
        deviation_hits = [
            {"head_id": int(r[0]), "ts": r[1], "app_torque": r[2],
             "reason": f"torque deviates > {cfg.mad_k}*MAD from head median {r[3]:.3f}"}
            for r in con.execute(
                f"""
                WITH caps AS (
                    SELECT head_id, ts, app_torque FROM cap_events
                    WHERE app_torque > 0 AND {where}
                ),
                med AS (
                    SELECT head_id, MEDIAN(app_torque) AS m FROM caps GROUP BY head_id
                ),
                mad AS (
                    SELECT c.head_id, m.m,
                           MEDIAN(ABS(c.app_torque - m.m)) AS mad
                    FROM caps c JOIN med m USING (head_id)
                    GROUP BY c.head_id, m.m
                )
                SELECT c.head_id, c.ts, c.app_torque, mad.m
                FROM caps c JOIN mad USING (head_id)
                WHERE mad.mad > 0
                  AND ABS(c.app_torque - mad.m) > ? * mad.mad
                ORDER BY c.ts
                """,
                params + [cfg.mad_k],
            ).fetchall()
        ]

    return ToolResult.ok(
        "anomalies",
        {
            "faults": faults,
            "threshold_hits": threshold_hits,
            "deviation_hits": deviation_hits,
            "counts": {
                "faults": len(faults),
                "threshold_hits": len(threshold_hits),
                "deviation_hits": len(deviation_hits),
            },
        },
        period=period,
        rows_scanned=len(faults) + len(threshold_hits) + len(deviation_hits),
        filters=[f"method={method}", f"band=[{cfg.torque_min}, {cfg.torque_max}]",
                 f"mad_k={cfg.mad_k}"],
        assumptions=["deviation uses median +/- k*MAD (robust); mean/sigma would let "
                     "extreme outliers inflate the band and hide themselves"],
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd python && python3 -m pytest tests/ -v`
Expected: PASS — 37 passed

- [ ] **Step 5: Commit**

```bash
git add python/analytics/tools/anomaly.py python/tests/test_anomaly.py
git commit -m "feat(analytics): anomalies() — threshold + robust MAD deviation

Deterministic, per the brief's requirement that WP2 tools be reliable for the
agent to call. No model, no training, no randomness.

The band is median +/- k*MAD rather than mean +/- k*sigma: a few extreme outliers
inflate sigma enough to hide themselves. The two methods are complementary —
threshold catches torque outside the machine's spec band, deviation catches a
head drifting from its own normal while still inside it.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 10: `trend()` — moving averages and Mann-Kendall drift

With three months of data this is where the real signal lives (spec §5.2): on a machine with 4 faults/day, drift matters more than failure counting.

**Files:**
- Create: `python/analytics/tools/trend.py`, `python/tests/test_trend.py`

**Interfaces:**
- Consumes: Tasks 2–3
- Produces: `trend(cfg, period=None, signal="torque", by="day", window=7) -> ToolResult`; `signal` ∈ `{"torque", "success_rate"}`. `values` = `{"series": [{head_id, bucket, value, rolling_mean, rolling_std}], "drift": [{head_id, tau, direction, drifting}]}`. Drift uses Mann-Kendall's tau over the per-head bucketed series; `drifting` is `abs(tau) >= 0.5`.

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_trend.py`:

```python
import duckdb
import pytest
from analytics.config import Config
from analytics.tools.trend import trend, mann_kendall_tau


def test_mann_kendall_detects_a_monotonic_rise():
    assert mann_kendall_tau([1, 2, 3, 4, 5]) == pytest.approx(1.0)


def test_mann_kendall_detects_a_monotonic_fall():
    assert mann_kendall_tau([5, 4, 3, 2, 1]) == pytest.approx(-1.0)


def test_mann_kendall_is_zero_for_flat_noise():
    assert abs(mann_kendall_tau([2.0, 2.0, 2.0, 2.0])) < 1e-9


def test_mann_kendall_needs_at_least_two_points():
    assert mann_kendall_tau([1.0]) == 0.0


@pytest.fixture
def drift_store(tmp_path):
    """Head 1's torque walks upward day by day; head 2 stays flat."""
    path = tmp_path / "drift.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE cap_events (
            machine_id VARCHAR NOT NULL, head_id SMALLINT NOT NULL, ts TIMESTAMP,
            cap_seq BIGINT NOT NULL, app_torque REAL, status REAL, delta INTEGER,
            is_fault BOOLEAN, aggregated BOOLEAN, is_reset BOOLEAN,
            UNIQUE (machine_id, head_id, cap_seq))
    """)
    seq = 0
    for day in range(1, 11):
        for head, torque in ((1, 1.90 + 0.02 * day), (2, 2.00)):
            seq += 1
            con.execute(
                "INSERT INTO cap_events VALUES ('MCC',?,?,?,?,0.0,1,false,false,false)",
                [head, f"2026-02-{day:02d} 12:00:00", seq, torque],
            )
    con.close()
    return str(path)


def test_flags_the_drifting_head_and_not_the_flat_one(drift_store):
    cfg = Config(store_path=drift_store)
    r = trend(cfg, period="2026-02", signal="torque", by="day", window=3)
    assert r.status == "ok"
    drift = {d["head_id"]: d for d in r.values["drift"]}
    assert drift[1]["drifting"] is True
    assert drift[1]["direction"] == "rising"
    assert drift[2]["drifting"] is False


def test_series_carries_rolling_statistics(drift_store):
    cfg = Config(store_path=drift_store)
    r = trend(cfg, period="2026-02", by="day", window=3)
    head1 = [s for s in r.values["series"] if s["head_id"] == 1]
    assert len(head1) == 10
    assert head1[-1]["rolling_mean"] is not None


def test_empty_period_is_insufficient(tiny_cfg):
    r = trend(tiny_cfg, period="2026-09")
    assert r.status == "insufficient_data"


def test_rejects_unknown_signal(tiny_cfg):
    r = trend(tiny_cfg, period="2026-02", signal="vibes")
    assert r.status == "error"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd python && python3 -m pytest tests/test_trend.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'analytics.tools.trend'`

- [ ] **Step 3: Write the implementation**

Create `python/analytics/tools/trend.py`:

```python
"""Trend and drift (brief WP2: moving averages, drift detection).

This is where the real signal lives. The machine's success rate is ~99.999%, so
counting failures says almost nothing; a head whose torque is slowly *walking*
away from its baseline is the finding that matters for the predictive maintenance
the brief names as motivation.

Drift uses the Mann-Kendall rank correlation (tau), not a linear regression:
it is non-parametric, makes no assumption that the walk is linear or the noise
Gaussian, and is deterministic. tau = +1 means every day rose on the previous;
-1 means every day fell.
"""
import pandas as pd

from analytics.result import ToolResult
from analytics.store import connect, period_clause

_SIGNALS = ("torque", "success_rate")
_BUCKETS = {"day": "DAY", "hour": "HOUR"}
DRIFT_TAU = 0.5     # |tau| at or above this counts as drifting


def mann_kendall_tau(values):
    """Kendall's tau-a: (concordant - discordant) / (n*(n-1)/2). Range [-1, 1]."""
    n = len(values)
    if n < 2:
        return 0.0
    s = 0
    for i in range(n - 1):
        for j in range(i + 1, n):
            if values[j] > values[i]:
                s += 1
            elif values[j] < values[i]:
                s -= 1
    return s / (n * (n - 1) / 2)


def trend(cfg, period=None, signal="torque", by="day", window=7):
    if signal not in _SIGNALS:
        return ToolResult.error("trend", f"signal must be one of {list(_SIGNALS)}, "
                                         f"got {signal!r}", period=period)
    if by not in _BUCKETS:
        return ToolResult.error("trend", f"by must be one of {sorted(_BUCKETS)}, "
                                         f"got {by!r}", period=period)

    con = connect(cfg)
    where, params = period_clause(period)
    unit = _BUCKETS[by]

    expr = ("AVG(app_torque)" if signal == "torque"
            else "COUNT(*) FILTER (WHERE status = ?) * 1.0 / COUNT(*)")
    sem = [] if signal == "torque" else [cfg.success_status]

    rows = con.execute(f"""
        SELECT head_id, DATE_TRUNC('{unit}', ts) AS bucket, {expr} AS value
        FROM cap_events
        WHERE app_torque > 0 AND {where}
        GROUP BY 1, 2
        HAVING COUNT(*) > 0
        ORDER BY 1, 2
    """, sem + params).fetchall()

    if not rows:
        return ToolResult.insufficient(
            "trend", f"no capping operations in period {period!r}", period=period
        )

    df = pd.DataFrame(rows, columns=["head_id", "bucket", "value"])
    df["rolling_mean"] = df.groupby("head_id")["value"].transform(
        lambda s: s.rolling(window, min_periods=1).mean()
    )
    df["rolling_std"] = df.groupby("head_id")["value"].transform(
        lambda s: s.rolling(window, min_periods=1).std()
    )

    drift = []
    for head, grp in df.groupby("head_id"):
        tau = mann_kendall_tau(list(grp["value"]))
        drift.append({
            "head_id": int(head),
            "tau": tau,
            "direction": "rising" if tau > 0 else "falling" if tau < 0 else "flat",
            "drifting": bool(abs(tau) >= DRIFT_TAU),
        })
    drift.sort(key=lambda d: -abs(d["tau"]))

    series = [
        {"head_id": int(r.head_id), "bucket": r.bucket, "value": r.value,
         "rolling_mean": None if pd.isna(r.rolling_mean) else float(r.rolling_mean),
         "rolling_std": None if pd.isna(r.rolling_std) else float(r.rolling_std)}
        for r in df.itertuples()
    ]

    return ToolResult.ok(
        "trend", {"series": series, "drift": drift},
        period=period, rows_scanned=len(series),
        filters=[f"signal={signal}", f"by={by}", f"window={window}"],
        assumptions=[f"drift is Mann-Kendall |tau| >= {DRIFT_TAU} over the per-head "
                     f"{by} series (non-parametric: assumes neither linearity nor "
                     "Gaussian noise)"],
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd python && python3 -m pytest tests/ -v`
Expected: PASS — 45 passed

- [ ] **Step 5: Commit**

```bash
git add python/analytics/tools/trend.py python/tests/test_trend.py
git commit -m "feat(analytics): trend() — rolling stats and Mann-Kendall drift detection

Where the real signal lives. Success rate is ~99.999%, so counting failures says
almost nothing; a head whose torque is slowly walking away from baseline is the
finding that matters for predictive maintenance.

Drift uses Mann-Kendall rank correlation rather than linear regression: it is
non-parametric, assuming neither a linear walk nor Gaussian noise, and it is
deterministic.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 11: `head_correlation()`

The brief: *"correlation checks among selected signals (e.g. performance of head 1 vs head 5)"* and *"which capping head behaves differently from the others?"*

**Files:**
- Create: `python/analytics/tools/correlation.py`, `python/tests/test_correlation.py`

**Interfaces:**
- Consumes: Tasks 2–3
- Produces: `head_correlation(cfg, period=None, heads=None, by="day") -> ToolResult`; `heads=None` → all discovered heads. `values` = `{"matrix": {head_id: {head_id: r}}, "outliers": [{head_id, mean_correlation}]}`. `outliers` is sorted ascending by mean correlation — the head least like its peers is first, answering "which head behaves differently?".

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_correlation.py`:

```python
import duckdb
import pytest
from analytics.config import Config
from analytics.tools.correlation import head_correlation


@pytest.fixture
def corr_store(tmp_path):
    """Heads 1 and 2 move together; head 3 moves opposite to both."""
    path = tmp_path / "corr.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE cap_events (
            machine_id VARCHAR NOT NULL, head_id SMALLINT NOT NULL, ts TIMESTAMP,
            cap_seq BIGINT NOT NULL, app_torque REAL, status REAL, delta INTEGER,
            is_fault BOOLEAN, aggregated BOOLEAN, is_reset BOOLEAN,
            UNIQUE (machine_id, head_id, cap_seq))
    """)
    seq = 0
    for day in range(1, 9):
        up = 1.90 + 0.02 * day
        down = 2.10 - 0.02 * day
        for head, torque in ((1, up), (2, up), (3, down)):
            seq += 1
            con.execute(
                "INSERT INTO cap_events VALUES ('MCC',?,?,?,?,0.0,1,false,false,false)",
                [head, f"2026-02-{day:02d} 12:00:00", seq, torque],
            )
    con.close()
    return str(path)


def test_correlated_heads_score_near_one(corr_store):
    cfg = Config(store_path=corr_store)
    r = head_correlation(cfg, period="2026-02")
    assert r.status == "ok"
    assert r.values["matrix"][1][2] == pytest.approx(1.0, abs=1e-6)


def test_anticorrelated_head_scores_near_minus_one(corr_store):
    cfg = Config(store_path=corr_store)
    r = head_correlation(cfg, period="2026-02")
    assert r.values["matrix"][1][3] == pytest.approx(-1.0, abs=1e-6)


def test_identifies_the_head_that_behaves_differently(corr_store):
    cfg = Config(store_path=corr_store)
    r = head_correlation(cfg, period="2026-02")
    assert r.values["outliers"][0]["head_id"] == 3


def test_selecting_a_head_pair(corr_store):
    cfg = Config(store_path=corr_store)
    r = head_correlation(cfg, period="2026-02", heads=[1, 3])
    assert sorted(r.values["matrix"]) == [1, 3]


def test_too_few_buckets_is_insufficient_not_a_crash(tiny_cfg):
    # The tiny store has a single day: correlation over one point is undefined.
    r = head_correlation(tiny_cfg, period="2026-02", by="day")
    assert r.status == "insufficient_data"
    assert "at least 2" in r.message
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd python && python3 -m pytest tests/test_correlation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'analytics.tools.correlation'`

- [ ] **Step 3: Write the implementation**

Create `python/analytics/tools/correlation.py`:

```python
"""Head-vs-head correlation (brief WP2).

Answers "compare head 1 and head 5" and, more usefully, "which capping head
behaves differently from the others?" -- a head whose mean correlation against
its peers is lowest is the one out of step with the machine.

Correlation is computed on the per-head bucketed torque series, so two heads
correlate when their torque moves together over time. A single bucket cannot
produce a correlation, so that is insufficient_data rather than a NaN.
"""
import pandas as pd

from analytics.result import ToolResult
from analytics.store import connect, discover_heads, period_clause

_BUCKETS = {"day": "DAY", "hour": "HOUR"}


def head_correlation(cfg, period=None, heads=None, by="day"):
    if by not in _BUCKETS:
        return ToolResult.error("head_correlation", f"by must be one of {sorted(_BUCKETS)}, "
                                                    f"got {by!r}", period=period)
    con = connect(cfg)
    where, params = period_clause(period)
    unit = _BUCKETS[by]

    if heads is None:
        heads = discover_heads(con)          # never assumes 36
    if len(heads) < 2:
        return ToolResult.insufficient(
            "head_correlation", f"need at least 2 heads, got {len(heads)}", period=period
        )

    placeholders = ",".join("?" for _ in heads)
    rows = con.execute(f"""
        SELECT head_id, DATE_TRUNC('{unit}', ts) AS bucket, AVG(app_torque) AS value
        FROM cap_events
        WHERE app_torque > 0 AND head_id IN ({placeholders}) AND {where}
        GROUP BY 1, 2 ORDER BY 2
    """, list(heads) + params).fetchall()

    if not rows:
        return ToolResult.insufficient(
            "head_correlation", f"no capping operations in period {period!r}", period=period
        )

    df = pd.DataFrame(rows, columns=["head_id", "bucket", "value"])
    wide = df.pivot(index="bucket", columns="head_id", values="value")
    if len(wide) < 2:
        return ToolResult.insufficient(
            "head_correlation",
            f"need at least 2 {by} buckets to correlate, got {len(wide)}",
            period=period, rows_scanned=len(rows),
        )

    corr = wide.corr()
    matrix = {
        int(a): {int(b): (None if pd.isna(corr.loc[a, b]) else float(corr.loc[a, b]))
                 for b in corr.columns}
        for a in corr.index
    }

    outliers = []
    for head, row in corr.iterrows():
        peers = row.drop(labels=[head]).dropna()
        if len(peers):
            outliers.append({"head_id": int(head), "mean_correlation": float(peers.mean())})
    outliers.sort(key=lambda o: o["mean_correlation"])

    return ToolResult.ok(
        "head_correlation", {"matrix": matrix, "outliers": outliers},
        period=period, rows_scanned=len(rows),
        filters=[f"heads={sorted(heads)}", f"by={by}"],
        assumptions=["heads correlate on their bucketed mean torque series; the head "
                     "with the lowest mean correlation to its peers is the one out of step"],
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd python && python3 -m pytest tests/ -v`
Expected: PASS — 50 passed

- [ ] **Step 5: Commit**

```bash
git add python/analytics/tools/correlation.py python/tests/test_correlation.py
git commit -m "feat(analytics): head_correlation() — pairwise matrix and the odd head out

Answers 'compare head 1 and head 5' and, more usefully, 'which head behaves
differently from the others?' — the head whose mean correlation against its peers
is lowest is the one out of step with the machine.

A single bucket cannot produce a correlation, so that returns insufficient_data
rather than a NaN that would propagate silently into a report.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 12: Real-data validation and spec reconciliation

Every tool has been tested against hand-built fixtures. Now run them against the real three months and pin the numbers, exactly as Plans 1–5 did with the oracle.

**Files:**
- Create: `python/tests/test_real_data.py` (marked `slow`, skipped when the store is absent)
- Create: `scripts/build_store.sh`
- Modify: `docs/validation-log.md`
- Modify: `docs/superpowers/specs/2026-07-11-agentic-analytics-reporting-design.md` (§5.4, §12)

**Interfaces:**
- Consumes: all tools (Tasks 4–11)

- [ ] **Step 1: Write the store-build script**

Create `scripts/build_store.sh`:

```bash
#!/usr/bin/env bash
# Build the cleaned event store from the raw month zips.
# Usage: scripts/build_store.sh <out.duckdb> [month.zip ...]
set -euo pipefail

OUT="${1:?usage: build_store.sh <out.duckdb> [month.zip ...]}"
shift
ZIPS=("$@")
[ ${#ZIPS[@]} -eq 0 ] && ZIPS=(telemetry_*.zip)

for z in "${ZIPS[@]}"; do
    dir="${z%.zip}"
    [ -d "$dir" ] || unzip -q "$z"
done

CSVS=()
for z in "${ZIPS[@]}"; do
    dir="${z%.zip}"
    while IFS= read -r f; do CSVS+=("$f"); done < <(find "$dir" -name '*.csv' | sort)
done

echo "cleaning ${#CSVS[@]} day-files into $OUT"
rm -f "$OUT"
./build/mas_monolith "$OUT" MCC 4 "${CSVS[@]}"
```

Make executable and run it:

```bash
chmod +x scripts/build_store.sh
scripts/build_store.sh events_3mo.duckdb
```

Expected: `monolith: 89 files, ... events, ... store holds <rows> rows`

- [ ] **Step 2: Write the real-data test**

Create `python/tests/test_real_data.py`:

```python
"""Real-data gate. Skipped unless events_3mo.duckdb exists (build it with
scripts/build_store.sh). This is the analytics equivalent of the oracle checks
that guarded Plans 1-5.
"""
import os
import pytest

from analytics.config import Config
from analytics.tools.overview import overview
from analytics.tools.success import success_rates
from analytics.tools.torque import torque_stats
from analytics.tools.trend import trend

STORE = "events_3mo.duckdb"
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
```

- [ ] **Step 3: Write the independent oracle cross-check**

Spec §10 requires the headline KPIs be verified by an implementation that shares
*no code* with the toolkit — the same discipline that caught the counter-reset bug in
Plan 5. This one re-derives the counts straight from the raw CSV, touching neither
DuckDB nor any toolkit SQL.

Create `python/oracle_kpi.py`:

```python
#!/usr/bin/env python3
"""Independent KPI oracle: recompute the headline counts from the raw CSV.

Shares no code with analytics/ -- no DuckDB, no toolkit SQL. If the toolkit's
SQL and this disagree, one of them is wrong, and the disagreement is the point.

Semantics under test (spec §3.2):
    real cap     := counter advanced AND torque > 0
    successful   := real cap AND status == 0
    failed       := status == 65
    no-load      := counter advanced AND status == 2 AND torque == 0
"""
import csv
import sys


def kpis(path):
    counts = {"capping_operations": 0, "successful": 0, "failed": 0, "no_load_cycles": 0}
    with open(path) as fh:
        r = csv.reader(fh)
        hdr = next(r)
        cidx = [i for i, h in enumerate(hdr) if "Count" in h]
        tidx = [i for i, h in enumerate(hdr) if "AppTorque" in h]
        sidx = [i for i, h in enumerate(hdr) if "Status" in h]
        prev = None
        for row in r:
            if len(row) <= max(cidx + tidx + sidx):
                continue                       # truncated row, as the C++ reader skips
            cur = [row[i] for i in cidx]
            if prev is not None:
                for k in range(len(cidx)):
                    if cur[k] == prev[k]:
                        continue               # head did not close
                    torque = float(row[tidx[k]])
                    status = float(row[sidx[k]])
                    if torque > 0:
                        counts["capping_operations"] += 1
                        if status == 0.0:
                            counts["successful"] += 1
                    if status == 65.0:
                        counts["failed"] += 1
                    if status == 2.0 and torque == 0:
                        counts["no_load_cycles"] += 1
            prev = cur
    return counts


if __name__ == "__main__":
    for p in sys.argv[1:]:
        print(p, kpis(p))
```

Add to `python/tests/test_real_data.py`:

```python
import glob

import oracle_kpi
from analytics.tools.overview import overview


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
```

- [ ] **Step 4: Run the full suite**

Run: `cd python && python3 -m pytest tests/ -v`
Expected: PASS — 55 passed (50 fixture tests + 5 real-data tests)

If the oracle and the design-time figures disagree, **stop and investigate** — do not
adjust the expected numbers to match. Those constants came from measuring the raw file
directly during brainstorming, and a disagreement means the semantics moved.

- [ ] **Step 5: Record the real numbers**

Run each tool against the real store and capture the output:

```bash
cd python && python3 - <<'PY'
from analytics.config import Config
from analytics.tools.overview import overview
from analytics.tools.success import success_rates
from analytics.tools.torque import torque_stats
from analytics.tools.trend import trend
from analytics.tools.idle import idle_periods
from analytics.tools.anomaly import anomalies

cfg = Config(store_path="../events_3mo.duckdb")
for period in ("2026-02", "2026-02..2026-04"):
    print(f"--- {period} ---")
    print("overview     ", overview(cfg, period).values)
    print("success      ", success_rates(cfg, period, by="overall").values)
    print("torque       ", torque_stats(cfg, period).values)
    print("anomalies    ", anomalies(cfg, period).values["counts"])
    d = trend(cfg, period).values["drift"]
    print("drifting     ", [x for x in d if x["drifting"]])
PY
```

Append a `## Plan 6 — Analytics Foundation` section to `docs/validation-log.md` recording, verbatim, the real figures for: total capping operations, successful/failed, no-load cycles, overall success rate, mean/min/max torque, anomaly counts, and which heads drift. Include the same caveats style as the existing entries.

- [ ] **Step 6: Reconcile the spec**

In `docs/superpowers/specs/2026-07-11-agentic-analytics-reporting-design.md`:

- **§5.4** — replace the "capping speed in the extractor" bullet with: capping speed is computed in SQL by `capping_speed()` (`analytics/tools/speed.py`); the extractor is unchanged and no schema column was added.
- **§12 open question 2 (reprocessing)** — mark **RESOLVED**: no reprocessing was needed, because capping speed is derived in SQL from events already persisted.

- [ ] **Step 7: Commit**

```bash
git add scripts/build_store.sh python/oracle_kpi.py python/tests/test_real_data.py \
        docs/validation-log.md \
        docs/superpowers/specs/2026-07-11-agentic-analytics-reporting-design.md
git commit -m "test(analytics): real-data gate across all three months, and spec reconciliation

Runs the toolkit against the real 89 day-files and pins the numbers, the same way
the oracle guarded Plans 1-5. The load-bearing assertions are the ones that catch
a semantics regression: success rate must stay above 99%, and mean successful
torque must stay inside 1.9-2.1 Nm. If no-load cycles ever leak back into the
capping-operation denominator, success collapses to ~56% and the mean craters
toward zero — both tests fail loudly.

Also reconciles the spec: capping speed is computed in SQL rather than the
extractor, so no schema column and no reprocessing were needed. Open question 2
is resolved.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Done when

- 72 C++ tests green (68 + 4 new semantics tests)
- 55 Python tests green, including the independent-oracle cross-check
- All seven WP2 tools return typed `ToolResult`s with provenance
- No tool hard-codes 36 heads, a path, or a torque band
- `docs/validation-log.md` records the real three-month figures
- The spec's §5.4 and §12 match what was actually built

## Not in this plan (Plan 7)

WP3 report agent, WP4 CLI, report templates, plots, sample reports, the end-to-end demo. Plan 7 consumes the `ToolResult` contract defined here and generates the LLM tool schema from these seven signatures.
