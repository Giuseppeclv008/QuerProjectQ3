"""Reference dedup: independent re-implementation of CapEventExtractor.
Used to cross-check the C++ core on real data (spec §11)."""
import csv
import sys

NUM_HEADS = 36
FAULT_CODES = {65.0}


def extract(path):
    last = [None] * NUM_HEADS
    events = []
    with open(path, newline="") as f:
        reader = csv.reader(f)
        next(reader)  # header
        for row in reader:
            if len(row) < 1 + NUM_HEADS * 3:
                continue
            ts = row[0]
            counts = [float(x) for x in row[1:1 + NUM_HEADS]]
            torque = [float(x) for x in row[1 + NUM_HEADS:1 + 2 * NUM_HEADS]]
            status = [float(x) for x in row[1 + 2 * NUM_HEADS:1 + 3 * NUM_HEADS]]
            for h in range(NUM_HEADS):
                c = round(counts[h])
                if last[h] is None:
                    last[h] = c
                    continue
                if c > last[h]:
                    delta = c - last[h]
                    events.append((h + 1, ts, c, torque[h], status[h],
                                   delta, status[h] in FAULT_CODES, delta > 1, False))
                    last[h] = c
                elif c < last[h]:
                    events.append((h + 1, ts, c, torque[h], status[h],
                                   0, status[h] in FAULT_CODES, False, True))
                    last[h] = c
    return events


if __name__ == "__main__":
    print(len(extract(sys.argv[1])))
