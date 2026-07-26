#!/usr/bin/env python3
"""Independent KPI oracle: recompute the headline counts from the raw CSV.

Shares no code with analytics/ -- no DuckDB, no toolkit SQL. If the toolkit's
SQL and this disagree, one of them is wrong, and the disagreement is the point.

Semantics under test (spec §3.2, amended by §3.2.1):
    real cap     := counter advanced AND torque > 0
    successful   := real cap AND status == 0
    failed       := real cap AND the reject bit is set, i.e. status is odd
    no-load      := counter advanced AND status == 2 AND torque == 0

The reject rule is written out longhand here rather than imported, because an
oracle that shares the toolkit's definition of failure cannot detect an error in
it. `status` is a bitmask: bit 0 is the reject signal, so 65 (Bad Closure +
reject) and 9 (No InTorque + reject) are both failures and 4 (No Closure, not
rejected) is not.
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
                        elif int(status) % 2 == 1:
                            counts["failed"] += 1
                    if status == 2.0 and torque == 0:
                        counts["no_load_cycles"] += 1
            prev = cur
    return counts


if __name__ == "__main__":
    for p in sys.argv[1:]:
        print(p, kpis(p))
