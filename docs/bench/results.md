# Benchmark results (median of 3 repeats)

| arch | n_workers | threads | files | clean_s | merge_s | total_s | events_per_s | peak_rss_mb | cpu_pct |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| mas | 1 | 1 | 1 | 3.424 | 2.081 | 5.503 | 139144 | 222.1 | 169.8 |
| mas | 1 | 1 | 7 | 16.993 | 10.606 | 27.595 | 141367 | 663.2 | 173.2 |
| mas | 1 | 1 | 28 | 91.337 | 65.194 | 156.404 | 139847 | 2693.6 | 183.8 |
| mas | 2 | 1 | 1 | 3.393 | 2.079 | 5.472 | 139933 | 246.4 | 170.1 |
| mas | 2 | 1 | 7 | 9.36 | 10.568 | 19.928 | 195756 | 582 | 337.9 |
| mas | 2 | 1 | 28 | 53.813 | 63.035 | 117.583 | 186019 | 2216.4 | 331.3 |
| mas | 4 | 1 | 1 | 3.404 | 2.105 | 5.493 | 139398 | 295.9 | 170.6 |
| mas | 4 | 1 | 7 | 7.816 | 10.817 | 18.658 | 209080 | 651.2 | 469.3 |
| mas | 4 | 1 | 28 | 38.144 | 63.323 | 101.072 | 216407 | 1745.5 | 651.6 |
| mas | 8 | 1 | 1 | 3.392 | 2.073 | 5.501 | 139195 | 399.3 | 174.9 |
| mas | 8 | 1 | 7 | 5.712 | 10.646 | 16.32 | 239033 | 869.7 | 707.3 |
| mas | 8 | 1 | 28 | 29.758 | 62.848 | 92.606 | 236191 | 1611.1 | 825.7 |
| mas | 16 | 1 | 1 | 3.472 | 2.107 | 5.552 | 137916 | 602 | 178.1 |
| mas | 16 | 1 | 7 | 5.742 | 10.635 | 16.397 | 237910 | 1055 | 701.2 |
| mas | 16 | 1 | 28 | 27.072 | 64.038 | 91.222 | 239774 | 2196.2 | 938 |
| mono-1T | 0 | 1 | 1 | 3.431 | 0 | 3.431 | 223174 | 80.4 | 113.7 |
| mono-1T | 0 | 1 | 7 | 18.251 | 0 | 18.251 | 213743 | 103.6 | 117.8 |
| mono-1T | 0 | 1 | 28 | 100.979 | 0 | 100.979 | 216606 | 229.6 | 117.2 |
| mono-MT | 0 | 2 | 1 | 3.489 | 2.242 | 5.813 | 131724 | 133.9 | 111.1 |
| mono-MT | 0 | 2 | 7 | 10.164 | 11.366 | 21.529 | 181198 | 320.1 | 160.1 |
| mono-MT | 0 | 2 | 28 | 55.689 | 70.55 | 127.352 | 171750 | 1527.8 | 158.4 |
| mono-MT | 0 | 4 | 1 | 3.48 | 2.215 | 5.696 | 134430 | 144 | 109.7 |
| mono-MT | 0 | 4 | 7 | 7.058 | 11.592 | 18.65 | 209170 | 249 | 241.3 |
| mono-MT | 0 | 4 | 28 | 37.903 | 68.508 | 106.097 | 206157 | 915.3 | 265.9 |
| mono-MT | 0 | 8 | 1 | 3.442 | 2.172 | 5.614 | 136393 | 151 | 110.9 |
| mono-MT | 0 | 8 | 7 | 6.975 | 11.808 | 18.652 | 209147 | 341.5 | 276.6 |
| mono-MT | 0 | 8 | 28 | 31.409 | 67.829 | 99.43 | 219980 | 735.7 | 278.9 |

Caveats: laptop thermals (no fan control), median-of-3, N=16 on 8 cores is a deliberate oversubscription point, merge phase reported separately; mono-MT uses a std::thread atomic-counter pool (dynamic load balancing, slightly fairer than PUSH/PULL round-robin).

## Data notes (real month, re-measured 2026-08-11 under the ts event identity)

- **The store used to discard real capping events, and this benchmark used to
  reward it.** The previous version of this file recorded that "days 16-24
  replay cap_seq ranges already emitted in days 1-15" and that
  `UNIQUE(machine_id, head_id, cap_seq)` "dedupes replayed sequences", so
  21,872,663 processed events "persist as 14,372,237 distinct rows". The replay
  hypothesis was never tested and is false: of head 1's 23,851 day-17 closures
  whose `cap_seq` collides with days 1-15, **18,721 carry a different torque**.
  They are distinct physical caps. The PLC's Count register resets and climbs
  again through values it has already used. Identity is now
  `(machine_id, head_id, ts)` — one head closes at most once per poll — and all
  28 day-files persist in full: **21,872,663 events, 21,872,663 rows**.
- **Every number in the table is therefore larger than the version it replaces,
  and that is the correction, not a regression.** The old run wrote 14.4M rows
  where this one writes 21.9M for the same input, so it was timing 66% of the
  work. 28-day medians, old -> new: mono-1T 87.5 -> 101.0 s, MAS N=16
  75.7 -> 91.2 s, mono-MT T=8 87.3 -> 99.4 s.
- **The correctness oracle now checks something.** `oracle_union.py` counted
  distinct `(head_id, cap_seq)` — precisely the quantity the defect leaves
  stable — so all 81 runs reported "oracle-exact" while a third of February was
  being dropped. It counts distinct `(head_id, ts)` now, and the 28-day
  expectation moved from 14,372,237 to 21,872,663. All 81 runs match it.
- **The merge cost stopped growing with N, and that is diagnostic.** Old
  merge_s at 28 days climbed with store count (N=1 35.1 s -> N=16 50.9 s);
  it is now flat (N=1 65.2, N=2 63.0, N=4 63.3, N=8 62.8, N=16 64.0 s). The
  old growth *was the defect doing work*: more stores meant more colliding
  `cap_seq` for `INSERT OR IGNORE` to resolve, and every resolution threw away
  a real closure. With disjoint sources the merge only moves rows, so its cost
  follows total volume and not how the volume is partitioned.
- **The scaling conclusion survives, slightly weaker.** Clean parallelises
  better than before (mono-1T 101.0 s -> MAS N=16 27.1 s, **3.73x**, up from
  3.50x, because there is more write work to spread). End-to-end MAS still tops
  out over mono-1T but at **1.11x** (91.2 s vs 101.0 s) rather than 1.16x.
  mono-MT still never meaningfully beats mono-1T at month scale, though its
  optimum moved from T=4 to T=8 (T=2 127.4, T=4 106.1, T=8 99.4 s) for the same
  reason: more write work per thread rewards more threads.
- **The merge is now 70% of MAS N=16's wall time** (64.0 s of 91.2 s), up from
  67%. The Amdahl wall of spec §14 Q4's "per-worker single-writer stores, merge
  at the sink" did not move; it got taller. `merge_from` is N sequential
  `INSERT OR IGNORE` passes, each probing the UNIQUE index once per row against
  a growing destination — ~22M probes that, sources now being disjoint, almost
  never find anything. Branch `perf/merge-set-based` replaces them with one
  hash-based `DISTINCT` over the union; **not yet measured**, because measuring
  it during this sweep would have falsified both.
- **`events_per_s` counts processed events**, identical input work for every
  config. Row persistence now equals it exactly — nothing is dropped between
  extraction and the store.
- **Initial dispatch is gated on worker registration** (`--workers N`): without
  the gate, ZMQ PUSH's slow-joiner behavior queued every file into the first
  connected worker and MAS timings measured a serialized pipeline (sweep #1,
  discarded).
- **`rows_per_s` uses a nominal 86,399 rows/day**; real days range 58,772 to
  86,401 raw rows, so it is approximate for the 7- and 28-day volumes —
  `events_per_s` is the measured throughput.
- **MAS `clean_s` includes worker spawn, ZMQ connect, and the registration
  wait**. Empirically small — v=1 clean_s is flat ~3.2-3.4 s across N=1..16 —
  but it is part of the measured number.

### Measurement integrity

- The two series are not a controlled comparison. Same machine and same
  harness, but the disk was in a different state (2.6 GB freed before this run)
  and it is a laptop without fan control. Shape — how it scales with N, where
  the wall sits — is reliable; absolute seconds across the two runs are
  indicative.
- The 30 MAS rows at volumes 1 and 7 were re-measured separately: during the
  first pass `build/` was rebuilt underneath the running sweep while preparing
  `perf/merge-set-based`, so some of those runs may have exercised the
  set-based merge. They were re-run through the same harness with the restored
  binaries (`run_bench.sh --only mas --volumes "1 7"`, added for this purpose)
  and spliced in. The monolith block finished before any rebuild and the MAS
  28-day block ran after the binaries were restored; neither was affected. The
  superseded file is kept as `bench/results.csv.contaminated`.
