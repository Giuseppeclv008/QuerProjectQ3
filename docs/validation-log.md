# Validation Log

## 2026-07-06 — Plan 2: DuckDB store, idempotent reprocessing (real data)

- File: telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02-01.csv (86,399 rows, 109 cols)
- `clean` → DuckDB run 1: wrote 765,711 events; store rows 765,711
- `clean` → DuckDB run 2 (same file): wrote 765,711 events; store rows **765,711** (unchanged — upsert)
- Python oracle events: 765,711 (match)
- Spec cross-refs: §6 schema + UNIQUE key, §10 idempotency backbone, ~765k/day Appendix A
- ts-format scan, all 28 day-files (streamed from zip, no extraction): 0 malformed timestamps of 2,391,250 rows — strict-CAST interim policy is safe on current data

## 2026-07-08 — Plan 3: ZeroMQ MAS, 2 workers × 2 day-files (real data)

- Files: 2026-02-01 (86,399 rows) and 2026-02-02 (86,399 rows) day-files.
- Oracle expectations: day 01 = 765,711; day 02 = 998,920; total = 1,764,631.
- `mas_coordinator` (2 workers, tcp://127.0.0.1:5557/5558): "dispatched 2 files: 2 ok, 0 failed, 1764631 events" (wall clock 6.952s).
- Per-worker stores merged via `mas_merge`: merged store holds 1,764,631 rows; second merge run unchanged (idempotent, spec §10).
- Counter continuity verified: day 01 ends at Count 118929, day 02 starts at 118929 -> UNIQUE(machine_id, head_id, cap_seq) is collision-free across days (spec §14 Q3: cumulative counter; day-boundary seeding loses no caps between these days).
- Spec cross-refs: §5.2 PUSH/PULL fabric, §8 ventilator/worker/sink + day-file work unit, §14 Q4 per-worker stores merged at sink.
