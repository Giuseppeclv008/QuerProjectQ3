# Benchmark results (median of 3 repeats)

| arch | n_workers | threads | files | clean_s | merge_s | total_s | events_per_s | peak_rss_mb | cpu_pct |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| mas | 1 | 1 | 1 | 3.233 | 1.847 | 5.078 | 150790 | 146.6 | 160 |
| mas | 1 | 1 | 7 | 16.341 | 9.473 | 25.815 | 151114 | 243 | 167.9 |
| mas | 1 | 1 | 28 | 80.502 | 35.101 | 115.705 | 189038 | 569.8 | 153.6 |
| mas | 2 | 1 | 1 | 3.212 | 1.851 | 5.063 | 151237 | 168 | 160.6 |
| mas | 2 | 1 | 7 | 8.75 | 9.664 | 18.396 | 212058 | 290.3 | 328.8 |
| mas | 2 | 1 | 28 | 48.702 | 44.821 | 93.935 | 232849 | 573.9 | 303 |
| mas | 4 | 1 | 1 | 3.232 | 1.887 | 5.108 | 149904 | 207.9 | 216.7 |
| mas | 4 | 1 | 7 | 7.034 | 9.708 | 16.736 | 233091 | 460.2 | 427 |
| mas | 4 | 1 | 28 | 32.117 | 50.458 | 82.511 | 265088 | 723.6 | 571.9 |
| mas | 8 | 1 | 1 | 3.231 | 1.847 | 5.077 | 150820 | 291.4 | 165.8 |
| mas | 8 | 1 | 7 | 5.141 | 9.697 | 14.838 | 262907 | 711.5 | 686.2 |
| mas | 8 | 1 | 28 | 26.951 | 50.769 | 77.95 | 280599 | 1129.8 | 786.6 |
| mas | 16 | 1 | 1 | 3.264 | 1.853 | 5.112 | 149787 | 455.4 | 172 |
| mas | 16 | 1 | 7 | 5.205 | 9.796 | 15.001 | 260050 | 874.2 | 687 |
| mas | 16 | 1 | 28 | 25.029 | 50.899 | 75.712 | 288893 | 1827.9 | 905.8 |
| mono-1T | 0 | 1 | 1 | 3.29 | 0 | 3.29 | 232739 | 80.7 | 106.3 |
| mono-1T | 0 | 1 | 7 | 17.506 | 0 | 17.506 | 222839 | 108.2 | 107.7 |
| mono-1T | 0 | 1 | 28 | 87.483 | 0 | 87.483 | 250022 | 173.5 | 108.4 |
| mono-MT | 0 | 2 | 1 | 3.321 | 1.949 | 5.265 | 145434 | 83.2 | 105.1 |
| mono-MT | 0 | 2 | 7 | 9.372 | 10.354 | 19.729 | 197730 | 149.1 | 155.2 |
| mono-MT | 0 | 2 | 28 | 47.866 | 44.112 | 91.681 | 238574 | 267.5 | 184.8 |
| mono-MT | 0 | 4 | 1 | 3.331 | 1.949 | 5.292 | 144692 | 88.2 | 105.1 |
| mono-MT | 0 | 4 | 7 | 6.365 | 10.447 | 16.789 | 232356 | 234 | 202.7 |
| mono-MT | 0 | 4 | 28 | 35.653 | 50.974 | 86.627 | 252492 | 341.6 | 236.5 |
| mono-MT | 0 | 8 | 1 | 3.328 | 1.958 | 5.285 | 144884 | 97.7 | 104.9 |
| mono-MT | 0 | 8 | 7 | 6.466 | 10.554 | 17.033 | 229027 | 326.4 | 274.9 |
| mono-MT | 0 | 8 | 28 | 33.671 | 53.443 | 87.336 | 250443 | 508.3 | 333.9 |

Caveats: laptop thermals (no fan control), median-of-3, N=16 on 8 cores is a deliberate oversubscription point, merge phase reported separately; mono-MT uses a std::thread atomic-counter pool (dynamic load balancing, slightly fairer than PUSH/PULL round-robin).

## Data notes (real month, measured during Task 5)

- **The month's Count counter reset mid-day-16**: days 16-24 replay cap_seq
  ranges already emitted in days 1-15 (day 17 contributes only 15 new rows out
  of 858,651 events); days 25-28 pass the old high-water mark. All 28
  day-boundary seams are continuous — the reset is only visible at month
  scale. The store's UNIQUE(machine_id, head_id, cap_seq) constraint dedupes
  replayed sequences identically for every architecture, so 21,872,663
  processed events persist as 14,372,237 distinct rows (28 days). The
  correctness oracle is therefore the distinct-pair union
  (`python/oracle_union.py`), which every one of the 81 runs matched exactly.
- **`events_per_s` counts processed events** (identical input work for every
  config); row-persistence differs from it only through the dedup above.
- **Initial dispatch is gated on worker registration** (`--workers N`): without
  the gate, ZMQ PUSH's slow-joiner behavior queued every file into the first
  connected worker and MAS timings measured a serialized pipeline (sweep #1,
  discarded). This sweep distributes work across all N workers.
- **`rows_per_s` uses a nominal 86,399 rows/day**; real days range 58,772 to
  86,401 raw rows, so it is approximate for the 7- and 28-day volumes —
  `events_per_s` is the measured throughput. 
- **The merge phase is the scaling wall**: clean time parallelizes well
  (28-day medians: mono-1T 87.5 s -> MAS N=8 27.0 s, 3.2x; N=16 25.0 s, 3.5x)
  but merging per-worker/per-thread stores into one costs ~35-54 s at month
  scale, rising with store count (MAS N=1 35 s -> N>=4 ~50-51 s; mono-MT T=8
  53 s). End-to-end: MAS tops out at 1.12-1.16x over mono-1T (N=8: 78.0 s vs
  87.5 s); mono-MT never meaningfully beats mono-1T at month scale (best
  1.01x at T=4, 86.6 s vs 87.5 s; T=2 is net slower). This is the measured
  cost of the spec §14 Q4 "per-worker single-writer stores, merge at the
  sink" resolution.
- **MAS `clean_s` includes worker spawn, ZMQ connect, and the registration
  wait** (`t_start` is stamped before workers fork; the window closes after
  the post-coordinator `wait`). Empirically small — v=1 clean_s is flat
  ~3.2-3.3 s across N=1..16 — but it is part of the measured number.
