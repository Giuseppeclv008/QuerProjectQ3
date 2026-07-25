"""Regenerate the golden KPI report. Run deliberately, then read the diff.

    cd python && ../.venv/bin/python -m tests.regen_golden

Kept out of the test itself on purpose: a test that writes its own expected
output cannot fail, and would re-bless a regression the moment the fixture was
deleted.
"""
import pathlib
import tempfile

import duckdb

from analytics.agent.executor import execute
from analytics.agent.router import canned_plan
from analytics.config import Config
from analytics.report import render
from tests.conftest import ROWS

GOLDEN = pathlib.Path(__file__).parent / "fixtures" / "golden_kpi_report.md"
FIXED_TIME = "2026-07-24T12:00:00Z"


def _build_store(path):
    con = duckdb.connect(path)
    con.execute("""
        CREATE TABLE cap_events (
            machine_id VARCHAR NOT NULL, head_id SMALLINT NOT NULL,
            ts TIMESTAMP, cap_seq BIGINT NOT NULL, app_torque REAL, status REAL,
            delta INTEGER, is_fault BOOLEAN, aggregated BOOLEAN, is_reset BOOLEAN,
            UNIQUE (machine_id, head_id, cap_seq))
    """)
    for m, h, ts, seq, tq, st in ROWS:
        con.execute("INSERT INTO cap_events VALUES (?,?,?,?,?,?,1,?,false,false)",
                    [m, h, ts, seq, tq, st, int(st) % 2 == 1])
    con.close()


def main():
    with tempfile.TemporaryDirectory() as tmp:
        store = tmp + "/tiny.duckdb"
        _build_store(store)
        cfg = Config(store_path=store, machine_id="MCC")
        ex = execute(cfg, canned_plan("kpi", "2026-02"))
        text = render.render(ex, cfg, tmp, render.summarise(ex),
                             generated_at=FIXED_TIME)
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(text)
    print(f"wrote {GOLDEN} ({len(text)} bytes) -- now read the diff")


if __name__ == "__main__":
    main()
