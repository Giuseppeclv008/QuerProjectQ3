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

## 2026-07-09 — Plan 4: chaos E2E, worker killed mid-run + coordinator death (real data)

- Files: day-files 2026-02-01, 2026-02-02, 2026-02-03 (first three by date). Oracle (per-file `clean` into a fresh DuckDB store, counts from its stderr "wrote N cap events" line): day 01 = 765,711; day 02 = 998,920; day 03 = 525,602; **total = 2,290,233**.

### Direction 1 — worker SIGKILLed mid-run (spec §11 criterion 1): PASS

- Command: `scripts/chaos_e2e.sh "$D"/telemetry_..._2026-02-01.csv "$D"/telemetry_..._2026-02-02.csv "$D"/telemetry_..._2026-02-03.csv` with `D=telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02` (2 workers on tcp://127.0.0.1:5571-5573, w1 killed with `kill -9` at t+2 s).
- Coordinator log (excerpt): `worker w1 joined` / `worker w2 joined` / `worker w1 dead (silent > 30000 ms)` / `re-dispatch ..._2026-02-03.csv (attempt 2)` / `re-dispatch ..._2026-02-02.csv (attempt 2)` / `re-dispatch ..._2026-02-01.csv (attempt 2)` / summary `dispatched 3 files: 3 ok, 0 failed, 2290233 events, 1 workers died`, exit 0.
- All three items were re-dispatched: ZMQ PUSH had round-robined every initial send onto the first-connected worker before the other's connect landed, so the killed worker held all three — the survivor absorbed the full set (`worker w2 done: 3 work items, store holds 2290233 rows`).
- Merge of both stores, dead worker's included: `merged 2 stores (0 skipped); dst holds 2290233 rows` — w1's store opened fine (killed ~2 s in; no rows or a committed-batch subset of w2's re-processed rows) and the idempotent upsert left the count unaffected.
- `PASS: merged 2290233 == oracle 2290233 (one worker killed mid-run)`. Wall clock 56 s first run, 57 s post-fix re-run (30 s death threshold dominates; identical outcome both runs).

### Direction 2 — coordinator death, orphan worker (spec §11 criterion 2): FAILED pre-fix at 121 s, PASSES post-fix at 61 s

- Command: `"$BUILD"/mas_worker tcp://127.0.0.1:5581 tcp://127.0.0.1:5582 tcp://127.0.0.1:5583 /tmp/w_orphan.duckdb w1 &` with nothing ever bound (per-second `kill -0` poll; pass = gone within 65 s).
- **Pre-fix FAIL (defect found by this E2E):** `run()` idle-exited on time — stderr `worker w1 done: 0 work items, store holds 0 rows` at t+61 s — but the *process* lived to t+121 s. Stack sample at t+75 s: main thread in `zmq_ctx_term -> zmq::ctx_t::terminate -> mailbox recv wait`, I/O thread still in `tcp_connecter_t::start_connecting`. Cause: `ZmqPushSink` coupled ZMQ_LINGER to `send_timeout_ms` (60000), and a connect-mode PUSH queues sends below HWM even with no peer ever present (SNDTIMEO cannot fire), so teardown waited out the full 60 s linger on ~61 undeliverable heartbeats. Invisible to the fake-transport unit suite (62/62 green throughout) — only this real-socket run could catch it.
- **Fix:** `ZmqPushSink` gained an explicit `linger_ms` parameter (default sentinel keeps the old coupling for all existing callers; coordinator unchanged); `mas_worker` passes `linger_ms=0` on its results + heartbeat sinks (protocol-safe: the coordinator STOPs only after every item is settled, and heartbeats are fire-and-forget). Regression-guarded by `ZmqTransport.ZeroLingerTeardownDropsUndeliverableQueueImmediately` (teardown with a queued undeliverable message must complete < 2 s; pre-fix behavior would hold it 60 s).
- **Post-fix:** `worker exited at t+61s` / `PASS: worker self-exited` / exit code 0 / stderr `worker w1 done: 0 work items, store holds 0 rows` — the 60-tick idle exit (60 × 1 s + hello) is now the process's actual lifetime, within the ~65 s budget.
- Full suite after fix: 63/63 PASS.
- Spec cross-refs: §6 death threshold 30 s + re-dispatch, §7 worker liveness (idle exit, send timeouts), §8 worker exit budget, §10 chaos test, §11 both success criteria.

## 2026-07-11 — Plan 5: benchmark sweep, mono(T) vs MAS(N) on the real month (8 cores, 16 GB)

- HEAD at run: 7de9ac7. Data: full month `telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02` (28 day-files, 1.6 GB extracted from the 42 MB zip).

### Correctness gate (spec §7): threaded == sequential == oracle — PASS

- `./build/mas_monolith /tmp/gate1.duckdb MCC…db 1 "$D"/*.csv` → `monolith: 28 files, 21872663 events, clean 88.372 s, merge 0.000 s, total 88.372 s, store holds 14372237 rows`
- Same with `8` threads → `…, clean 26.629 s, merge 53.486 s, total 80.115 s, store holds 14372237 rows` — identical rows.
- **Finding (data): the month's Count counter resets mid-day-16.** Days 16-24 replay cap_seq ranges from days 1-15 (day 17 adds 15 new rows out of 858,651 events); days 25-28 pass the old high-water mark; all 28 day-boundary seams are continuous, so Plan 3's two-day continuity measurement (2026-07-08 entry above) could not see it. UNIQUE(machine_id, head_id, cap_seq) dedupes replays identically for every architecture: 21,872,663 processed events persist as 14,372,237 rows. The multi-day oracle is therefore the distinct-(head,cap_seq) union — `python/oracle_union.py`, independent of every C++ binary under test; it reproduces day-1 = 765,711, days-1-2 = 1,764,631 (the Plan 3 entry's number), and the exact 28-day store count 14,372,237.

### Distribution defect found and fixed before evidence was accepted (spec §5.2/§8)

- Sweep #1 showed MAS clean time flat across N. Probe (4 workers, 7 files, even with a 2 s settle sleep): one worker took all 7 items — ZMQ PUSH's slow-joiner capture: the coordinator PUSH-sent every item at start, before worker connects landed. The 2026-07-09 entry above had already recorded this symptom in the chaos run ("PUSH had round-robined every initial send onto the first-connected worker") — it read as benign load-skew there because re-dispatch masked it; at benchmark scale it serialized the MAS axis. Correctness was never affected (all counts oracle-exact).
- Fix (054cc3b): registration gate — `CoordinatorConfig.expected_workers` holds the initial dispatch until N workers say hello (Plan 4's hello-at-entry heartbeat), `--workers N` CLI flag, degraded/abort timeout semantics, 3 fake-time unit tests. Post-fix probe: 2/2/2/1 distribution across 4 workers, 4 joined, stores sum == 7-day union oracle. Sweep #1 discarded.

### Full sweep (sweep #2, gated): 81/81 runs oracle-exact — PASS

- Command: `./bench/run_bench.sh` (matrix: mono T∈{1,2,4,8} + MAS N∈{1,2,4,8,16}, volumes {1,7,28} day-files, 3 repeats; per-run assertion rows == union oracle; since 7de9ac7 each MAS run also asserts exactly N pre-dispatch registrations).
- Oracle totals: `oracle[1 days] = 765711`, `oracle[7 days] = 3900837`, `oracle[28 days] = 14372237`; `sweep complete: 81 rows in bench/results.csv`, exit 0.
- Headline medians, 28-day volume (full table + 4 plots in `docs/bench/`): mono-1T total **87.483 s**; best MAS total **75.712 s at N=16** (1.16×; N=8 77.950 s, 1.12×); best mono-MT total **86.627 s at T=4** (1.01×; T=2 is net slower than mono-1T). Clean phase alone scales 3.2× at N=8 (26.951 s) and 3.5× at N=16 (25.029 s); the sink-side store merge costs 35-54 s at month scale, rising with store count — the measured Amdahl wall of spec §14 Q4's per-worker single-writer stores. Conclusion recorded in `docs/bench/results.md`: the MAS architecture's value on one box is crash isolation (see the 2026-07-09 chaos entry) and horizontal scale-out headroom, not single-machine wall-clock.
- Caveats (full list in `docs/bench/results.md`): laptop thermals; N=16 on 8 cores is a deliberate oversubscription point; MAS clean_s includes worker spawn + connect + registration wait (measured flat ~3.2-3.3 s at v=1 across N); rows_per_s uses a nominal 86,399 rows/day; cpu_pct divides by the coordinator process's own wall time; sweep #2 predates the joined==N assertion (added 7de9ac7) — degraded-start mislabeling assessed low-risk (sub-second observed registration vs 10 s window, smooth N-curve) and structurally impossible for future runs.

### Regression close-out

- `cmake --build build -j 8` clean; `./build/unit_tests` → **68/68 PASS** (65 at plan-writing time + 3 registration-gate tests added by 054cc3b); `python -m pytest` (project venv) → **4 passed** (oracle + 3 bench_plots).
- Spec cross-refs: §5 matrix/metrics/oracle, §6 plots/table/caveats, §7 correctness gate, §9 success criteria 1-3.
