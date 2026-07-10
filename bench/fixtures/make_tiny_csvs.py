#!/usr/bin/env python3
"""Two deterministic 109-column day-file fixtures for monolith smokes.

Shape matches the real telemetry files (ts + 36 counts + 36 torques + 36
statuses). Day 1 takes head 1's counter 1 -> 2, day 2 CONTINUES it 2 -> 3 —
mirroring real day-files, whose machine counter is monotonic across days.
(cap_seq IS that counter value; if day 2 replayed 1 -> 2 the idempotent
UNIQUE(machine, head, cap_seq) store would dedupe the second event, and a
2-file run would report 2 events but hold only 1 row — caught at execution
time on 2026-07-10.) Expected totals: 1 event per file, 2 events and 2 rows
for both files together.
"""
import sys, pathlib

def write_fixture(path: pathlib.Path, ts0: str, ts1: str,
                  c0: int, c1: int) -> None:
    header = ",".join(["ts"] + [f"c{i}" for i in range(1, 109)])
    def row(ts: str, head1_count: int) -> str:
        counts = [str(head1_count)] + ["0"] * 35
        torques = ["2.0"] * 36
        stats = ["2.0"] * 36
        return ",".join([ts] + counts + torques + stats)
    path.write_text(header + "\n" + row(ts0, c0) + "\n" + row(ts1, c1) + "\n")

def main() -> None:
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    write_fixture(out / "tiny-day1.csv", "2026-02-01T00:00:00",
                  "2026-02-01T00:00:01", 1, 2)
    write_fixture(out / "tiny-day2.csv", "2026-02-02T00:00:00",
                  "2026-02-02T00:00:01", 2, 3)
    print(f"wrote {out}/tiny-day1.csv {out}/tiny-day2.csv")

if __name__ == "__main__":
    main()
