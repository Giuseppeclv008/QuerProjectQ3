"""A tiny hand-built store with known values, so every expectation is checkable by eye.

4 heads (NOT 36 -- head count must be discovered, spec §3.5).
Head 1: 3 successful caps (status 0, torque > 0)
Head 2: 1 successful, 2 rejects (status 65 Bad Closure, status 9 No InTorque)
Head 3: 2 no-load cycles (status 2, torque 0) -- never a capping operation
Head 4: nothing at all -- a head that never fires
"""
import duckdb
import pytest

from analytics.config import Config

CAP_EVENTS_DDL = """
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
"""

ROWS = [
    # machine_id, head_id, ts,                     cap_seq, torque, status
    ("MCC", 1, "2026-02-01 00:00:00", 1, 2.00, 0.0),
    ("MCC", 1, "2026-02-01 00:00:10", 2, 2.10, 0.0),
    ("MCC", 1, "2026-02-01 00:00:20", 3, 1.90, 0.0),
    ("MCC", 2, "2026-02-01 00:00:00", 1, 2.00, 0.0),
    ("MCC", 2, "2026-02-01 00:00:30", 2, 1.99, 65.0),   # fault
    ("MCC", 2, "2026-02-01 00:00:40", 3, 1.95, 9.0),    # reject: No InTorque
    ("MCC", 3, "2026-02-01 00:00:00", 1, 0.00, 2.0),    # no-load
    ("MCC", 3, "2026-02-01 00:00:01", 2, 0.00, 2.0),    # no-load
]


@pytest.fixture
def tiny_store(tmp_path):
    path = tmp_path / "tiny.duckdb"
    con = duckdb.connect(str(path))
    con.execute(CAP_EVENTS_DDL)
    for m, h, ts, seq, tq, st in ROWS:
        con.execute(
            "INSERT INTO cap_events VALUES (?,?,?,?,?,?,1,?,false,false)",
            [m, h, ts, seq, tq, st, int(st) % 2 == 1],
        )
    # head 4 exists as a machine head but emitted nothing -- it is absent from the table
    con.close()
    return str(path)


@pytest.fixture
def tiny_cfg(tiny_store):
    return Config(store_path=tiny_store, machine_id="MCC")
