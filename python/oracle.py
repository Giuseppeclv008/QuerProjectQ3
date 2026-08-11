"""Reference dedup: independent re-implementation of CapEventExtractor.
Used to cross-check the C++ core on real data (spec §11)."""
import csv
import sys

NUM_HEADS = 36


def is_reject(status):
    """Bit 0 of the AROL status bitmask — mirrors mas::is_reject in CapEvent.hpp.

    This file used to test `status in {65.0}`, the pre-Plan-6 reading, kept long
    after the C++ moved to the bitmask. Nothing caught it because the only
    cross-check, validate_real.py, compared event *counts*, and is_fault does not
    change how many events are emitted. So the correction the project is proudest
    of had no independent oracle: status 9 (No InTorque) and every other odd code
    read as clean here while the C++ called them rejects.

    `% 2 != 0`, not `== 1`: Python floors and C++ truncates, so a negative status
    yields 1 here and -1 there. Comparing against zero agrees in both.
    """
    return int(status) % 2 != 0


def extract(path):
    """Cap events for one raw day-file, in (row asc, head asc) order.

    Tuple: (head_id, ts, cap_seq, torque, status, delta, is_fault, aggregated,
    reset) — the CapEvent field order.
    """
    last = [None] * NUM_HEADS
    events = []
    skipped = 0
    with open(path, newline="") as f:
        reader = csv.reader(f)
        next(reader)  # header
        for row in reader:
            if len(row) < 1 + NUM_HEADS * 3:
                skipped += 1
                continue
            ts = row[0]
            # CsvRawReader.cpp counts a malformed numeric cell into skipped_ and
            # keeps reading. float() raising here instead meant the oracle died
            # on exactly the dirty input the C++ is built to survive, so the
            # cross-check could not be run when it mattered most.
            try:
                counts = [float(x) for x in row[1:1 + NUM_HEADS]]
                torque = [float(x) for x in row[1 + NUM_HEADS:1 + 2 * NUM_HEADS]]
                status = [float(x) for x in row[1 + 2 * NUM_HEADS:1 + 3 * NUM_HEADS]]
            except ValueError:
                skipped += 1
                continue
            for h in range(NUM_HEADS):
                c = round(counts[h])
                if last[h] is None:
                    last[h] = c
                    continue
                if c > last[h]:
                    delta = c - last[h]
                    events.append((h + 1, ts, c, torque[h], status[h],
                                   delta, is_reject(status[h]), delta > 1, False))
                    last[h] = c
                elif c < last[h]:
                    events.append((h + 1, ts, c, torque[h], status[h],
                                   0, is_reject(status[h]), False, True))
                    last[h] = c
    extract.skipped = skipped
    return events


extract.skipped = 0


if __name__ == "__main__":
    print(len(extract(sys.argv[1])))
