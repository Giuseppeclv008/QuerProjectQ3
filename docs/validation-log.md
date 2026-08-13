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
- **[SUPERSEDED 2026-08-11 — the "replay" reading below is false; see "Event identity" at the end of this file. The events were distinct closures and the key was discarding them.]** **Finding (data): the month's Count counter resets mid-day-16.** Days 16-24 replay cap_seq ranges from days 1-15 (day 17 adds 15 new rows out of 858,651 events); days 25-28 pass the old high-water mark; all 28 day-boundary seams are continuous, so Plan 3's two-day continuity measurement (2026-07-08 entry above) could not see it. UNIQUE(machine_id, head_id, cap_seq) dedupes replays identically for every architecture: 21,872,663 processed events persist as 14,372,237 rows. The multi-day oracle is therefore the distinct-(head,cap_seq) union — `python/oracle_union.py`, independent of every C++ binary under test; it reproduces day-1 = 765,711, days-1-2 = 1,764,631 (the Plan 3 entry's number), and the exact 28-day store count 14,372,237.

### Distribution defect found and fixed before evidence was accepted (spec §5.2/§8)

- Sweep #1 showed MAS clean time flat across N. Probe (4 workers, 7 files, even with a 2 s settle sleep): one worker took all 7 items — ZMQ PUSH's slow-joiner capture: the coordinator PUSH-sent every item at start, before worker connects landed. The 2026-07-09 entry above had already recorded this symptom in the chaos run ("PUSH had round-robined every initial send onto the first-connected worker") — it read as benign load-skew there because re-dispatch masked it; at benchmark scale it serialized the MAS axis. Correctness was never affected (all counts oracle-exact).
- Fix (054cc3b): registration gate — `CoordinatorConfig.expected_workers` holds the initial dispatch until N workers say hello (Plan 4's hello-at-entry heartbeat), `--workers N` CLI flag, degraded/abort timeout semantics, 3 fake-time unit tests. Post-fix probe: 2/2/2/1 distribution across 4 workers, 4 joined, stores sum == 7-day union oracle. Sweep #1 discarded.

### Full sweep (sweep #2, gated): 81/81 runs oracle-exact — PASS

> **[SUPERSEDED 2026-08-11.** Every figure in this section is computed on the store as it existed under `UNIQUE(machine_id, head_id, cap_seq)`, which was discarding real closures across the PLC's counter reset. See **"Event identity: the counter key was discarding real closures"** at the end of this file for the measurement and the corrected values.**]**

- Command: `./bench/run_bench.sh` (matrix: mono T∈{1,2,4,8} + MAS N∈{1,2,4,8,16}, volumes {1,7,28} day-files, 3 repeats; per-run assertion rows == union oracle; since 7de9ac7 each MAS run also asserts exactly N pre-dispatch registrations).
- Oracle totals: `oracle[1 days] = 765711`, `oracle[7 days] = 3900837`, `oracle[28 days] = 14372237`; `sweep complete: 81 rows in bench/results.csv`, exit 0.
- Headline medians, 28-day volume (full table + 4 plots in `docs/bench/`): mono-1T total **87.483 s**; best MAS total **75.712 s at N=16** (1.16×; N=8 77.950 s, 1.12×); best mono-MT total **86.627 s at T=4** (1.01×; T=2 is net slower than mono-1T). Clean phase alone scales 3.2× at N=8 (26.951 s) and 3.5× at N=16 (25.029 s); the sink-side store merge costs 35-54 s at month scale, rising with store count — the measured Amdahl wall of spec §14 Q4's per-worker single-writer stores. Conclusion recorded in `docs/bench/results.md`: the MAS architecture's value on one box is crash isolation (see the 2026-07-09 chaos entry) and horizontal scale-out headroom, not single-machine wall-clock.
- Caveats (full list in `docs/bench/results.md`): laptop thermals; N=16 on 8 cores is a deliberate oversubscription point; MAS clean_s includes worker spawn + connect + registration wait (measured flat ~3.2-3.3 s at v=1 across N); rows_per_s uses a nominal 86,399 rows/day; cpu_pct divides by the coordinator process's own wall time; sweep #2 predates the joined==N assertion (added 7de9ac7) — degraded-start mislabeling assessed low-risk (sub-second observed registration vs 10 s window, smooth N-curve) and structurally impossible for future runs.

### Regression close-out

- `cmake --build build -j 8` clean; `./build/unit_tests` → **68/68 PASS** (65 at plan-writing time + 3 registration-gate tests added by 054cc3b); `python -m pytest` (project venv) → **4 passed** (oracle + 3 bench_plots).
- Spec cross-refs: §5 matrix/metrics/oracle, §6 plots/table/caveats, §7 correctness gate, §9 success criteria 1-3.

## 2026-07-24 — Plan 6: Analytics foundation, WP2 toolkit on the real three months (real data)

> **[SUPERSEDED 2026-08-11.** Every figure in this section is computed on the store as it existed under `UNIQUE(machine_id, head_id, cap_seq)`, which was discarding real closures across the PLC's counter reset. See **"Event identity: the counter key was discarding real closures"** at the end of this file for the measurement and the corrected values.**]**

- HEAD at run: 654c491 (branch `feat/agentic-analytics`). Store: `scripts/build_store.sh events_3mo.duckdb` over all three month zips → `monolith: 89 files, 55132433 events, clean 76.154 s, merge 133.593 s, total 209.746 s, store holds 20347822 rows` (369 MB). The 55.1M→20.3M gap is the counter-reset dedup the Plan 5 entry above characterised (UNIQUE(machine_id, head_id, cap_seq) collapses replays identically for every architecture).
- Python suite (project venv): **85 passed** — 80 fixture tests + 5 real-data tests (`python/tests/test_real_data.py`, skipped when the store is absent). C++ suite unchanged this plan (Task 1 semantics fix landed with its own tests earlier on the branch).

### Independent oracle cross-check (spec §10): toolkit SQL == raw-CSV re-derivation — PASS

- `python/oracle_kpi.py` recomputes the headline counts straight from the raw CSV — no DuckDB, no toolkit SQL, no shared code — the same discipline that caught the Plan 5 distribution defect. On `2026-02-01.csv` it reproduces the design-time figures **exactly**: `successful=427643, failed=4, no_load_cycles=337772, capping_operations=427802 (=427643+155+4)`. The month's toolkit counts contain the day's (`overview` caps/no-load ≥ the oracle day), tying the SQL to the independent oracle. The Task-1 status-semantics fix is therefore correct on real data, not just on fixtures.

### Headline KPIs, `overview()` / `success_rates()` / `torque_stats()` / `capping_speed()`

> **[SUPERSEDED 2026-08-11.** Every figure in this section is computed on the store as it existed under `UNIQUE(machine_id, head_id, cap_seq)`, which was discarding real closures across the PLC's counter reset. See **"Event identity: the counter key was discarding real closures"** at the end of this file for the measurement and the corrected values.**]**

| metric | 2026-02 | 2026-02..2026-04 |
|---|---|---|
| capping operations (torque>0) | 6,672,649 | 11,908,148 |
| successful (status 0) | 6,669,339 | 11,902,090 |
| failed (status 65) | 371 | 585 |
| no-load cycles (status 2, τ=0) | 3,774,599 | 8,433,525 |
| heads discovered | 1–36 (n=36) | 1–36 (n=36) |
| overall success rate | **0.999944** | **0.999951** |
| mean successful torque (Nm) | 1.9986 | 2.0104 |
| median / min / max τ (Nm) | 1.998 / 1.282 / 2.556 | 1.998 / 1.282 / 2.556 |
| stddev successful τ (Nm) | 0.0158 | 0.0606 |
| capping speed (pieces/hour, mean of day-buckets) | 11,121 | 8,409 |
| validation: counter_resets / null_τ / invalid_τ | 36 / 0 / 68 | 36 / 0 / 129 |
| ts range | 02-01 08:43:30 → 02-28 15:59:59 | 02-01 08:43:30 → 04-30 16:59:59 |

- **success_rate is `successful / (successful + failed)`, per the locked spec §3.2** — 0.9999, healthy, as the brief predicted (~99.999%). **Finding (data), fixed on-branch (654c491):** the shipped `success_rates()` originally divided by *total* capping operations, which agrees with the spec formula only when every cap is status 0 or 65 — true for the `tiny_store` fixture, so Task 5's tests never saw the gap. Real data carries ~2,939 caps/month whose status is neither (see next); dividing by total read 0.9995. Now spec-compliant; the load-bearing regression test still holds: if no-load cycles ever re-entered the denominator, success collapses to ~56%.
- **Finding (data): capping operations with unanticipated statuses.** Among torque>0 caps in 2026-02: status 0 → 6,669,339; **status 2 with τ>0 → 2,926** (contradicts the no-load reading, which is status 2 *with zero torque*); status 65 → 371; status 9 → 12; status 4 → 1. Together the non-{0,65} tail is ~0.04% of caps. Excluded from the success denominator (neither success nor fault); logged as spec Open Question #4 to confirm with the course/AROL — same footing as OQ#1 (status-encoding is inferred, not documented by AROL).

### `anomalies()`, `trend()`, `idle_periods()`

> **[SUPERSEDED 2026-08-11.** Every figure in this section is computed on the store as it existed under `UNIQUE(machine_id, head_id, cap_seq)`, which was discarding real closures across the PLC's counter reset. See **"Event identity: the counter key was discarding real closures"** at the end of this file for the measurement and the corrected values.**]**

- **anomalies** (band [1.5, 2.5], k=3): 2026-02 `{faults: 371, threshold_hits: 68, deviation_hits: 678325}`; 3-month `{faults: 585, threshold_hits: 129, deviation_hits: 1734460}`. Faults == the status-65 count (consistent). Threshold hits == `invalid_torque` (both are the out-of-band caps). **Caveat:** robust MAD deviation flags ~10% of caps (678k/6.67M) — the successful-torque spread is extremely tight (σ≈0.016 Nm), so median±3·MAD is a narrow band and normal process jitter clears it. This is the detector behaving as specified on a very-low-variance signal, not a defect; a real deployment would widen `mad_k` or gate deviation on a minimum absolute delta. Noted for Plan 7 report-agent tuning.
- **trend / drift** (Mann-Kendall, |τ|≥0.5, daily torque): **0 heads drift** over 2026-02 and over all three months. Expected for a machine holding 2.0 Nm to ±0.016: day-to-day mean torque is essentially flat, so no monotone walk. The tool ran clean across the 3-month range (`status ok`, 36 drift entries), which is the assertion the real-data test pins.
- **idle_periods** (min 300 s): 2026-02 → 12,276 periods, 26,949,710 s total; 3-month → 21,802 periods, 137,465,461 s total. Sustained no-load runs are common, consistent with 337k no-load cycles/day.

### Validation-process notes (two plan-code fixes needed to run real data)

- `scripts/build_store.sh` assumed every month zip extracts into a `telemetry_.../` subfolder; the Mar/Apr zips drop day-files flat into the cwd, so the first run silently built February only (28 files). Fixed to collect day-files whether flat or in a subdir (verified: 89 files across 02/03/04).
- `python/tests/test_real_data.py` looked for the store at `events_3mo.duckdb` (i.e. under `python/`), but `build_store.sh` and the plan's own record-numbers script put it in the repo root; the tests silently skipped. Fixed to `../events_3mo.duckdb`.
- Spec reconciled (§5.4, §12 OQ#2 RESOLVED): capping speed is SQL in `capping_speed()`, extractor unchanged, no schema column, no reprocessing.

## 2026-07-26 — Plan 7: WP3 report agent and WP4 CLI, end-to-end on the real three months

> **[SUPERSEDED 2026-08-11.** Every figure in this section is computed on the store as it existed under `UNIQUE(machine_id, head_id, cap_seq)`, which was discarding real closures across the PLC's counter reset. See **"Event identity: the counter key was discarding real closures"** at the end of this file for the measurement and the corrected values.**]**

**Store:** `events_3mo.duckdb` — 20,347,822 rows, machine `MCC`, 36 heads,
2026-02-01 08:43:30 → 2026-04-30 16:59:59.

**Command (one command reproduces all three reports):**

    scripts/demo.sh

Runtime 8.4 s for all three reports over 20.3 M rows. 12/12 tool steps returned
`ok`. Output committed under `docs/reports/`:

| directory | verb | period |
|---|---|---|
| `kpi-2026-02` | `report kpi` | 2026-02 |
| `drift-2026-02_2026-04` | `report drift` | 2026-02..2026-04 |
| `anomalies-2026-02` | `report anomalies` | 2026-02 |

### Every number in the KPI report, reconciled by hand

> **[SUPERSEDED 2026-08-11.** Every figure in this section is computed on the store as it existed under `UNIQUE(machine_id, head_id, cap_seq)`, which was discarding real closures across the PLC's counter reset. See **"Event identity: the counter key was discarding real closures"** at the end of this file for the measurement and the corrected values.**]**

Each figure below was re-derived with a direct DuckDB query written independently
of the toolkit, then compared against the committed `report.md`.

    .venv/bin/python -c "
    import duckdb
    con = duckdb.connect('events_3mo.duckdb', read_only=True)
    FEB = ['MCC', '2026-02-01', '2026-03-01']
    print(con.execute('''
      SELECT COUNT(*) FILTER (WHERE status = 0)                    AS successful,
             COUNT(*) FILTER (WHERE CAST(status AS BIGINT) %% 2 = 1) AS rejected_newbit,
             COUNT(*) FILTER (WHERE status = 65)                   AS rejected_old,
             COUNT(*)                                              AS capping_ops
      FROM cap_events
      WHERE machine_id = ? AND ts >= ? AND ts < ? AND app_torque > 0
    ''', FEB).fetchone())"

| figure | report | hand query | ✓ |
|---|---|---|---|
| capping operations | 6,672,649 | 6,672,649 | ✓ |
| heads | 1–36 | 1–36 | ✓ |
| time range | 02-01 08:43:30 → 02-28 15:59:59 | same | ✓ |
| no-load cycles (`status=2 AND app_torque=0`) | 3,774,599 | 3,774,599 | ✓ |
| torque outside 1.5–2.5 band | 68 | 68 | ✓ |
| successful (`status = 0`) | 6,669,339 | 6,669,339 | ✓ |
| **rejected (reject bit, Task 1)** | **383** | **383** | ✓ |
| rejected under the OLD `status == 65` rule | — | 371 | — |
| success rate | 99.9943% | 6,669,339 / (6,669,339+383) = 99.99426% | ✓ |
| weakest head | 29 @ 99.9660% over 185,349 | 29, 185349, 99.96601% | ✓ |
| throughput | 11,121.0817 pieces/h over 25 buckets | 6,672,649 / 24 / 25 = 11,121.0817 | ✓ |

**The Task 1 bitmask change is confirmed live in `success_rates`:** February
reports **383** rejected closures, not the 371 the old `status == 65` rule found.
Over the full three months the same comparison is **600** (new) against 585
(old). Both match the figures measured when Plan 7 was written. The headline rate
barely moves (0.999944 → 0.999943) — the point of the change is classification
correctness, not the headline.

### Three reporting defects found by this reconciliation, and fixed (commit 7a44358)

> **[SUPERSEDED 2026-08-11.** Every figure in this section is computed on the store as it existed under `UNIQUE(machine_id, head_id, cap_seq)`, which was discarding real closures across the PLC's counter reset. See **"Event identity: the counter key was discarding real closures"** at the end of this file for the measurement and the corrected values.**]**

Every underlying number was correct. The prose around three of them was not, and
none of the three is reproducible on the tiny test fixture.

1. **2,927 closures were silently outside the rate.** `success_rate`'s
   denominator is successful + rejected, but the report printed those two counts
   beside a capping-operations total they do not sum to: 6,669,339 + 383 against
   6,672,649. The gap is 2,926 closures with `status = 2` and torque > 0, plus one
   with `status = 4` — neither clean nor rejected, so they carry no pass/fail
   verdict. The report now states this and the arithmetic closes.
2. **Two indistinguishable drift findings.** The drift plan trends both torque
   and success_rate, and both rendered the identical sentence. The signal is now
   named: *Drift (torque)* and *Drift (success_rate)*.
3. **A false "odd head out".** `head_correlation` returns every head ranked by
   mean correlation — not a filtered outlier set — and the renderer named the
   first one. All 36 heads correlate above 0.9999, so the report asserted *"Head
   24 has the lowest mean correlation to its peers (1.000)"*. It now reports
   agreement when the heads are indistinguishable at the printed precision.

### Anomalies report

> **[SUPERSEDED 2026-08-11.** Every figure in this section is computed on the store as it existed under `UNIQUE(machine_id, head_id, cap_seq)`, which was discarding real closures across the PLC's counter reset. See **"Event identity: the counter key was discarding real closures"** at the end of this file for the measurement and the corrected values.**]**

| figure | report |
|---|---|
| rejected closures | 383 (agrees with the KPI report on the same period) |
| outside the configured torque band | 68 (agrees) |
| beyond their head's robust median ± 3·MAD band | 678,325 |
| weakest day | 2026-02-05 @ 99.9851% over 94,248 ops |

### Drift report

No head exceeds the Mann-Kendall |tau| ≥ 0.5 threshold on either signal over the
three months. Most variable head: 9 (sigma 0.0612 Nm about a median of 1.997 Nm).
Head agreement: all 36 heads at mean correlation 0.9999–1.0000.

### Agentic path, router fallback (no API key) — PASS

    env -u ANTHROPIC_API_KEY scripts/arol ask \
      "which capping head behaves differently from the others, and is it drifting?" \
      --period 2026-02..2026-04 --config /tmp/arol-demo.json --out /tmp/arol-ask -v

The report is still produced, 4/4 steps `ok`, and the limits section discloses
both the attempt and the reason:

> **Planning.** planning failed: the call failed: "Could not resolve
> authentication method…"; the keyword router selected the drift plan (3 keyword
> match(es)).

Note the SDK constructs a client without credentials and fails at *call* time, so
the path that actually fires is the call-failed branch rather than the no-client
branch. Both degrade to the router. The router's keyword match picked the drift
plan from the question, which is the right plan for it.

`-v` was also fixed here: it had raised the ROOT logger, so markdown-it emitted a
line per parse rule per line of the report and buried the tool calls. That run
printed hundreds of lines; it now prints thirteen — the plan, every tool call
with its arguments, both fallbacks with their reasons, and the files written.

### Agentic path against the live API — NOT RUN

`ANTHROPIC_API_KEY` was not available in this environment, so the `plan (llm)`
path has never been exercised against the real API. Every planner and narrator
test injects a fake client by design. **This is the one part of Plan 7 that
remains unverified end-to-end**, and it is where a wrong parameter name would
surface as a 400. To close it:

    export ANTHROPIC_API_KEY=...
    scripts/arol ask "which capping head behaves differently, and is it drifting?" \
      --period 2026-02..2026-04 --out /tmp/arol-ask -v

Expect `plan (llm): [...]` in the log and `narrative source: llm` in the report,
then confirm every number in Findings also appears in `trace.json`.

### Per-head reject concentration (the presentation's headline finding)

> **[SUPERSEDED 2026-08-11.** Every figure in this section is computed on the store as it existed under `UNIQUE(machine_id, head_id, cap_seq)`, which was discarding real closures across the PLC's counter reset. See **"Event identity: the counter key was discarding real closures"** at the end of this file for the measurement and the corrected values.**]**

The three-month per-head figures quoted on slide 10 of
`docs/presentation/outline.md`, and the query that produced them:

    .venv/bin/python -c "
    import duckdb
    con = duckdb.connect('events_3mo.duckdb', read_only=True)
    q = '''SELECT head_id, COUNT(*) AS ops,
                  COUNT(*) FILTER (WHERE CAST(status AS BIGINT) % 2 = 1) AS rejects
           FROM cap_events WHERE machine_id = 'MCC' AND app_torque > 0
           GROUP BY head_id ORDER BY rejects DESC LIMIT 4'''
    print(con.execute(q).fetchall())"

| head | capping ops | rejects | success rate |
|---:|---:|---:|---:|
| **29** | 330,809 | **75** | 99.9773% |
| 32 | 330,709 | 37 | 99.9888% |
| 36 | 330,732 | 37 | 99.9888% |
| 35 | 330,804 | 37 | 99.9888% |

600 rejects spread over 36 heads is a mean of **16.7 per head**. Head 29 carries
**75** — 4.5x the mean, and twice the next-worst head — on an ops count within
0.03% of its peers. The machine-level rate of 99.9943% hides this completely.

This is the one finding in the deck that is not visible in a committed report:
the KPI report names head 29 for February only, and no committed report carries
three-month per-head rejects. The query above is the evidence.

### Test suite

207 passed (Python), output pristine. C++ 73/73.

## 2026-07-26 — Local model support: qwen2.5:7b via Ollama, measured

**Hardware:** Apple M3, 8 CPU / 10 GPU cores, 16 GB unified memory.
**Model:** `qwen2.5:7b` (Q4, 4.7 GB) on Ollama 0.12.3, `num_ctx=8192`,
`temperature=0`.

This closes, against a local model, the gap the Plan 7 log recorded as open: the
agentic path had never been exercised against a live model of any kind. It is
*not* a substitute for the hosted-API run, which remains unexercised — the
request shapes differ.

### Prompt sizes that drive the design

| | chars | ~tokens |
|---|---:|---:|
| tier 3, full tool schema | 7,390 | 1,847 |
| plan JSON schema (flat) | 1,976 | 494 |
| planner system prompt | 875 | 218 |
| **tier 3 floor, before the question** | **10,241** | **2,560** |
| tier 2, one line per tool | 1,632 | 408 |
| tier 1, classify | 65 | 16 |

Ollama's default `num_ctx` is **2048** and it truncates silently rather than
erroring, so the default would clip the planner's schema and present as a stupid
model. `Config` now rejects `num_ctx < 4096` when `provider == "ollama"`.

### Tier 1 — classify. 5/6 correct; the keyword router got 0/6

Six naturally-phrased questions, none containing a router keyword:

| question | router | qwen2.5:7b | correct? |
|---|---|---|---|
| which head is worst? | DEFAULT(kpi) | drift | debatable |
| is head 12 getting worse? | DEFAULT(kpi) | drift | ✓ |
| any anomalies in February? | DEFAULT(kpi) | anomalies | ✓ |
| should I schedule maintenance on any head? | DEFAULT(kpi) | anomalies | ✓ |
| sta peggiorando qualche testa? | DEFAULT(kpi) | drift | ✓ |
| how fast are we producing? | DEFAULT(kpi) | kpi | ✓ |

The router matched **no** keyword in any of the six and defaulted to KPI every
time. Including the Italian question. ~2 s per call after a 36 s model load.

**A 0.5B is not enough.** `qwen2.5:0.5b` returned a structurally valid report
type all six times but got roughly one right, and on *"any anomalies in
February?"* it answered `drift` — worse than the router, which matches that
literal keyword. Structured outputs guarantee the shape, not the sense.

### Tier 3 — full plan. The flat schema costs 3 of 6 plans

| schema | accepted by `validate_step` | time per plan |
|---|---:|---:|
| flat union (Anthropic's requirement) | **3/6** | 25–55 s |
| per-tool (`anyOf`, one branch per tool) | **6/6** | 10–27 s |

All three flat-schema failures were the same error:

    REJECTED: tool 'trend' takes no argument 'outcome';
              it accepts ['by', 'period', 'signal', 'window']

The flat object shows all nine arguments on every step, so nothing stops the
model attaching one tool's argument to another. Per-tool branches make that
ungrammatical. It is also faster, having no nulls to emit. Anthropic keeps the
flat shape because its structured outputs require every property in `required`;
every other provider gets `per_tool`.

### End to end on the real store

    scripts/arol ask "which capping head behaves differently from the others,
                      and is it drifting?" \
      --period 2026-02..2026-04 --provider ollama --model qwen2.5:7b

| tier | plan the model produced | steps ok | wall clock |
|---|---|---|---:|
| `plan` | `head_correlation`, `trend` | 2/2 | 3 m 17 s |
| `classify` | chose `drift` → its 4-step canned plan | 4/4 | 2 m 57 s |
| `select` | `head_correlation`, `trend` | 2/2 | 2 m 56 s |

All three planned sensibly. Wall clock is dominated by narration, not planning
(classify plans in ~2 s).

### Narration is the weak spot, and it is now caught

Three runs out of three, the 7B returned an announcement of findings instead of
findings:

> "The analysis of the success rate for heads 1 through 36 during February to
> April 2026 reveals several key insights and potential issues. Here's a summary
> of the findings from both the correlation matrix and drift analysis tools:"

No bullet, and the promised list never arrives. **The check is the bullet, not
the number** — that reply contains digits ("36", "2026"), so a digit check would
have passed it, while a good one-line finding may legitimately carry none.

Rejected narration falls back to `render.summarise()`, and the report says so:

> - **Narration.** the model's findings carried no bullet; it announced findings
>   rather than stating them.

The fallback output is strictly better than what was rejected:

> - **Head agreement.** All 36 heads track each other closely (mean correlation
>   0.9999-1.0000); none is out of step.
> - **Drift (torque).** No head exceeds the Mann-Kendall drift threshold in this
>   period.

Both reports carried `narrative source: template, plan source: llm` — the model
was used where it was good and rejected where it was not, without a human
noticing.

### Isolated check on hallucinated numbers

Given a single `success_rates` result and asked to narrate it, the 7B produced
four accurate bullets and **invented no numbers**. It did misread `lowest_head:
29` as *"the lowest head count recorded during a failure was 29"* — the number is
right, the meaning is not. A grounding check catches fabricated figures but not
misread ones; clearer field naming in the payload would do more here than any
verifier.

### Test suite

228 passed (Python), output pristine. C++ 73/73. 18 new tests, all against
injected fake clients — the suite still makes no network call and needs no
daemon.

## 2026-08-11 — Event identity: the counter key was discarding real closures

Supersedes the "replay dedup" reading recorded in the 2026-07-10 entry above.
That entry states days 16-24 "replay cap_seq ranges from days 1-15" and that
`UNIQUE(machine_id, head_id, cap_seq)` "dedupes replays identically for every
architecture". The replay hypothesis was never tested. It is false.

### The measurement that settles it

For head 1, every day-17 closure whose `cap_seq` also appears on days 1-15,
compare the torque of the two rows:

```
head 1, days 01-15: 302,339 distinct cap_seq
head 1, day 17:      23,851 events
  colliding cap_seq:            23,851
    identical torque:            5,130   (a true replay would be all of these)
    DIFFERENT torque:           18,721   (distinct physical closures)
```

Sample collisions:

```
cap_seq 124817: 2026-02-01T21:37:15 τ=1.997  vs  2026-02-16T16:00:03 τ=2.000
cap_seq 124818: 2026-02-01T21:37:18 τ=2.002  vs  2026-02-16T16:00:06 τ=2.000
cap_seq 124819: 2026-02-01T21:37:20 τ=2.000  vs  2026-02-16T16:00:08 τ=2.002
```

The 5,130 "identical" are coincidence — torque clusters near 2.0 with three
decimals. Day 17 produced 858,651 events across 36 heads against a February
mean of ~781k/day: a normal production day, not a retransmission. The PLC's
Count register reset mid-day-16 and climbed again through ranges it had already
used.

### What it cost

| scope | events extracted | rows persisted | discarded |
|---|---:|---:|---:|
| February, 28 files | 21,872,663 | 14,372,237 | 34% |
| three months, 89 files | 55,132,433 | 20,347,822 | 63% |
| February *inside* the three-month store | — | 10,450,551 | a further 3.9M |

The last row is the sharpest: February holds 14,372,237 rows when built alone
and 10,450,551 inside the three-month store. 3.9M February closures were
evicted by March/April rows reusing their counter values, and because
`INSERT OR IGNORE` keeps whichever row arrives first, which side survived was
decided by thread scheduling and merge order. Same input, same count, different
contents.

### Why no gate caught it

`oracle_union.py` counted distinct `(head_id, cap_seq)` — exactly the quantity
the defect leaves stable. It was written to reproduce the store's behaviour
rather than to check it, so 81 of 81 benchmark runs reported "oracle-exact"
throughout.

`oracle.py` could not have helped either: it still classified rejects as
`status == 65`, three plans after the C++ moved to the bitmask, and nothing
noticed because `validate_real.py` compared only event *counts* — and `is_fault`
does not change how many events there are. The correction the project is
proudest of had no independent oracle at all.

### The fix

Identity is now `(machine_id, head_id, ts)`. A head closes at most once per poll
— caps missed between polls arrive as one event with `delta > 1` — so a
timestamp names the observation. Verified on the pool: 86,399 rows and 86,399
distinct timestamps per day-file, and the 28 files are contiguous and
non-overlapping (`2026-01-31T16:00:00` … `2026-02-28T15:59:59`, each starting
one second after the previous ends). Reprocessing a file still deduplicates
exactly, so §10 idempotency holds, and the MAS still does not need to see files
in timestamp order. Stores written under the old key are refused at open rather
than silently reused.

`oracle_union.py` now counts distinct `(head_id, ts)`. `validate_real.py`
compares all nine fields. Its first run found a second defect: `CsvEventStore`
wrote 6 significant digits, so raw cells like `2.0020000000000002` were exported
as `2.002`.

### Cross-check, 2026-02-01

```
oracle events: 765711 (rows skipped: 0)
cpp events:    765711
MATCH: 765711 events, all 9 fields
```

This is the first time `is_reject` has been checked against an independent
oracle on real data.

### Store rebuilt, reports regenerated

`events_3mo.duckdb` rebuilt from all 89 day-files under the new key:
**55,132,433 rows**, against 20,347,822 before — 2.71x, and exactly the number
of events the extractor emits. Nothing is discarded now. Per-month: 2026-01
119,984 (the 16:00 offset of the first file), 2026-02 21,971,506, 2026-03
11,409,247, 2026-04 21,631,696. 36 heads, and zero duplicate
`(machine_id, head_id, ts)`.

Built in stages (February, March, then April in two halves) because only 2.2 GB
of disk was free and a single pass needs ~5 GB. The store is append-mode and the
key makes loading idempotent and order-independent, so the staged result is
identical to a single run. The April run that hit `No space left on device`
failed cleanly and left the store readable at exactly the 33,300,411 rows it
held before — the `BEGIN`/`COMMIT` added in this branch is what made that a
clean abort instead of a partial write.

What the three committed reports had been reporting, February:

| | on the residue | rebuilt |
|---|---:|---:|
| rows scanned | 10,450,551 | 21,971,506 |
| capping operations | 6,672,649 | 14,824,304 |
| no-load cycles | 3,774,599 | 7,141,531 |
| rejected closures | 383 | 748 |
| torque outside band | 68 | 130 |
| **counter resets** | **36** | **145** |
| beyond robust band | 678,325 | 1,612,634 |
| idle | 7,486.0 head-hours | 11,551.3 head-hours |
| throughput | 11,121 pieces/hour | 27,985 pieces/hour |
| **period covered** | **2026-02-01 08:43:30 →** | **2026-02-01 00:00:09 →** |

Two rows deserve reading twice.

**The period.** The old report opened February at 08:43:30. February does not
start at 08:43. Those hours existed in the raw pool and had been evicted from
the store, so the report quietly described a shorter month than the one it
claimed to cover.

**Counter resets: 36, always 36.** February and the full three months both
reported exactly 36 — one per head, i.e. one reset event, in 89 days. The
figure was stable because the reset markers were themselves being deduped away
by the key that the resets had made ambiguous. The rebuilt store finds 145 in
February alone.

Throughput moves for two reasons at once: more rows, and `capping_speed` no
longer dividing a day's closures by a flat 24 h.

### The other half of the identity proof

The 2026-08-11 entry above shows the old key collapsed distinct closures. The
complementary check — that the new one does not — was run on the rebuilt store:

```sql
SELECT COUNT(*) FROM (
  SELECT machine_id, head_id, ts FROM cap_events GROUP BY 1,2,3 HAVING COUNT(*) > 1
);
-- 0, over 55,132,433 rows
```

Upstream of the store the same property holds in the raw pool: 86,399 rows per
day-file, 86,399 distinct timestamps. `docs/analytics-methods.md` claimed the
PLC "can emit two rows with the same timestamp", which is true across heads —
36 share every poll — and false within one. The idle query's `cap_seq`
tie-breaker is therefore belt-and-braces, not load-bearing.

### One row is one poll, not one cap

No analytics tool reads `delta` or `aggregated`; everything is `COUNT(*)`. A row
whose `delta` is 3 stands for three caps applied between two polls and is
counted once. This predates the key change, but the change removes the ambiguity
it was hiding behind, so it is worth a number rather than a shrug.

Measured over three real day-files (2026-02-01..03):

```
rows (non-reset):   2,290,233
SUM(delta):         2,290,271
undercount:                38   (0.0017% of real caps)
rows with delta > 1:       38   (one in ~60,000)
```

At ~0.2 caps/s/head against a 1 Hz poll, a poll almost never catches two caps.
0.0017% is far below the point where rewriting every tool to `SUM(delta)` would
buy more correctness than it risks, so `overview` now declares it in its
assumptions instead.

### Merge optimisation, measured (branch `perf/merge-set-based`)

`merge_from` called once per source is N sequential `INSERT OR IGNORE` passes,
each probing the UNIQUE index per row against a growing destination — ~22M
probes that, the sources now being disjoint, almost never find anything.
`merge_all` unions every source and deduplicates once.

A/B on the merge alone: 8 source stores built once from the 28 February
day-files, both binaries alternated over the same sources so thermal drift
could not land on one side.

| | median of 3 | rows |
|---|---:|---:|
| `merge_from` in a loop | 65.867 s | 21,872,663 |
| `merge_all` | **22.792 s** | 21,872,663 |

**2.89x, 43.1 s saved, identical row counts in all six runs.** Row equality is
the load-bearing check: a faster merge that loses rows is the defect this branch
exists to prevent, in a new costume.

**[SUPERSEDED 2026-08-13 — the full sweep it asks for exists, on actively-cooled
hardware (i7-13700H): measured end-to-end, MAS N=16 is 3.83x over mono-1T there.
The M2-frame figures in this paragraph (27.1 clean / 64.0 merge / 91.2 total /
1.11x) were later shown thermally unstable and never became measurable on that
chassis; see the resweep entry at the end of this file.]**
Projected end-to-end, NOT yet measured: MAS N=16 at 28 days is 27.1 s clean +
64.0 s merge = 91.2 s. At ~23 s of merge it would be ~50 s, i.e. ~2.0x over
mono-1T instead of 1.11x. That projection needs a full sweep before it is
written down as a result.

## 2026-08-13 — Interleaved A/B: the clean-time anomaly is the machine, and the mono-MT/MAS gap does not survive it

**HEAD:** `638478b`. **Hardware:** `Mac14,2` — MacBook Air M2, 8 cores, **no
fan**. That last fact turns out to be the whole story.

Two questions the PR #9 sweep left open: whether `clean_s` is repeatable within a
session, and what the mono-MT/MAS gap is now that mono-MT's merge clock stops
before its own cleanup (`638478b`).

**Method.** One binary set, built before the run and untouched during it. Four
rounds, each running mono-1T, mono-MT T=8 and MAS N=16 over the same 28 February
day-files, in that order every time. Interleaving is the point: any drift in the
machine hits all three configurations in the same round, so it appears as a trend
across rounds rather than as a difference between architectures. All 12 runs
produced exactly 21,872,663 rows.

### `clean_s` is repeatable on one core and not on eight

| config | r1 | r2 | r3 | r4 | median | spread |
|---|---:|---:|---:|---:|---:|---:|
| mono-1T | 105.4 | 107.4 | 105.7 | 108.8 | 106.6 | **3%** |
| mono-MT T=8 | 31.3 | 34.2 | 33.6 | 38.0 | 33.9 | **21%** |
| MAS N=16 | 30.6 | 40.6 | 36.9 | 47.0 | 38.8 | **53%** |

Same binary, same input, same twenty minutes. The single-core baseline is flat to
3% while both parallel configurations climb round on round — MAS by 53% from
first to last. This is thermal accumulation on a fanless chassis, and it is not a
caveat about precision: it means a parallel configuration's `clean_s` on this
machine records *when in the sweep it ran*, not how fast it is.

**This settles the "unexplained" inflation.** The PR #9 sweep reported `clean_s`
7-34% higher than the sweep before it for every parallel configuration and none
for mono-1T, with identical clean-path code, and presented 1.84x as a lower bound
on the reasoning that the inflation could only depress the ratio. The reasoning
does not hold: the sign of the effect is set by run order, not by the branch.
Nothing was inflated *by the branch*; the two sweeps sampled different points on
a thermal curve. **1.84x is not a floor. It is a number with tens of percent of
uncertainty**, and the recorded ±0 on it should be read as such.

### The 20.9 s gap is an artifact of the SIGTERM restart

| | mono-MT T=8 | MAS N=16 | gap |
|---|---:|---:|---:|
| recorded (PR #9) | 76.26 | 55.39 | **20.9 s** |
| interleaved, median of 4 | 70.4 | 72.6 | **−2.1 s** |

Per round the gap is +4.3, +2.2, −4.8, −6.3 — **the sign flips**. The two
architectures are tied inside the noise of this machine, and neither the
"architectures do not converge" reading nor the earlier "~4 s" projection
survives.

Where the 20.9 s came from is recorded in `docs/bench/results.md` itself: that
sweep was killed by SIGTERM at 65 of 81 rows, the completed monolith block was
kept, and the MAS block was re-run in a fresh session. So mono-MT's numbers come
from the tail of a long hot run and MAS's from a cold start. The caveat was
written down; the conclusion was drawn across it anyway. Against the table above,
mono-MT's recorded `clean_s` of 43.31 is high and MAS's 29.27 is low — exactly
the direction a hot block versus a cold block predicts.

Median merges, interleaved and with the clock fixed: mono-MT 35.8 s, MAS 33.7 s.
The recorded 32.4 / 25.4 split had two causes, both now removed — mono-MT was
charged for unlinking its own per-thread stores, and the two blocks were measured
in different thermal states.

### What still stands, and what does not

Unaffected: every correctness result, `merge_all`'s 2.89x on its own benchmark,
and the structural finding that merge cost stopped growing with source count.
`clean_s` for mono-1T is stable enough to trust.

Not established on this hardware: any end-to-end speedup ratio quoted to three
significant figures, and any comparison between two parallel architectures
measured in different sessions. The replacement is a full sweep on a machine with
active cooling — the numbers in `bench/results.csv` should be read as shape, not
as seconds, until then.

Raw data, all 12 runs:

| round | arch | clean_s | merge_s | total_s | rows |
|---|---|---:|---:|---:|---:|
| 1 | mono-1T | 105.4 | 0.0 | 105.4 | 21,872,663 |
| 1 | mono-MT T=8 | 31.3 | 35.1 | 66.5 | 21,872,663 |
| 1 | mas N=16 | 30.6 | 31.6 | 62.2 | 21,872,663 |
| 2 | mono-1T | 107.4 | 0.0 | 107.4 | 21,872,663 |
| 2 | mono-MT T=8 | 34.2 | 42.4 | 76.6 | 21,872,663 |
| 2 | mas N=16 | 40.6 | 33.8 | 74.4 | 21,872,663 |
| 3 | mono-1T | 105.7 | 0.0 | 105.7 | 21,872,663 |
| 3 | mono-MT T=8 | 33.6 | 32.3 | 66.0 | 21,872,663 |
| 3 | mas N=16 | 36.9 | 33.8 | 70.7 | 21,872,663 |
| 4 | mono-1T | 108.8 | 0.0 | 108.8 | 21,872,663 |
| 4 | mono-MT T=8 | 38.0 | 36.4 | 74.4 | 21,872,663 |
| 4 | mas N=16 | 47.0 | 33.7 | 80.7 | 21,872,663 |
---

## CUDA cleaning pipeline and the three-way benchmark

The dedup was ruled a poor GPU fit in the 2026-07-04 spec (§3: "GPU acceleration
is optional stretch for analytics only, not the cleaning core") on the premise
that it is a sequential per-head scan. It is not. Every branch of
`CapEventExtractor::process` ends with `last = c`, and the held branch is entered
only when `c == *last` — so `last_count_[h]` after row `i` is always
`llround(count[i][h])`, and the transform never reads state older than one row.

`tests/test_cap_event_extractor_flat.cpp` is that claim as a test: the
element-wise `extract_flat` and the shipped stateful extractor produce identical
`CapEvent` vectors — all nine fields — on the edge cases and on a real day-file
(765,711 events). It runs with no GPU.

Two timing modes, because at GPU speeds the DuckDB write is two orders of
magnitude larger than the transform and would flatten every arch into the same
number. `clean` is the comparison; `e2e` is the deployment truth. Measured on
the M3 at one day-file: `mono-1T` cleans in 0.47 s and takes 3.2 s end to end,
so the store is 85% of the wall clock.

### The vectorized Python contender is slower than the naive loop

The expectation going in was that `clean_vectorized.py` would beat `oracle.py`
by a wide margin. It does not: median of three at one day-file, `py-naive`
1.246 s against `py-numpy` 1.793 s. The cause is not vectorization failing to
pay — it is float parsing.

`oracle.py` uses `float()`, which is correctly rounded. pandas' default C parser
uses `xstrtod`, which is not: on the real pool it lands one ulp off on values
like `2.002`, and the differential test caught it at event 8055 of
2026-02-01. `float_precision="high"` is bit-identical to the default (66,553
differing cells on that same day-file), so only `float_precision="round_trip"`
agrees with `oracle.py` — and round_trip costs 1.18 s of the 1.79 s against
0.34 s for the wrong-but-fast path.

The remaining ~0.6 s is materializing 765,711 Python tuples, which numpy does
not remove either. So for this transform, at this output shape, vectorizing
Python buys nothing: the cost is correct parsing plus object materialization,
and both survive the rewrite. Reading the columns as strings and converting with
`numpy.astype(float64)` is bit-exact and slightly cheaper (0.98 s), which would
narrow the gap but not close it.

The wrong-but-fast parse was not kept. Spec §10 R2 applies to the Python
contender the same way it applies to the CUDA one: a parse that is one ulp out
is a bug to fix, not a tolerance to widen. Note that it would not have been
caught by the sweep's own cross-arch gate — the Count columns are integers and
parse exactly in every mode, so the event *counts* agree; only the torque and
status carried on each event differ.

### Numbers: pending, for CUDA

The development machine is an Apple M3 with no NVIDIA GPU and no `nvcc`, so the
CUDA path has never been compiled, let alone measured. `cuda_clean_main.cpp` is
plain C++ and does compile here; `CudaCleaner.cu` does not. It is written, and
its correctness gate (`mas_cuda_clean --verify`, bitwise against
`CapEventExtractorFlat`) runs as part of the sweep. To close this:

    cmake -S . -B build -DMAS_BENCH_ONLY=ON -DMAS_ENABLE_CUDA=ON
    cmake --build build --config Release
    python bench/run_bench_cuda.py --data telemetry_..._2026-02.zip

`py-naive` is measured at the 1-day volume only — 28 day-files is roughly two
hours of interpreted loop — and its larger volumes are linear extrapolations,
labelled as such. The transform is O(rows) with no cross-file state, so the
extrapolation is sound for `clean`; it is not claimed for `e2e`.

`bench/results_cuda.csv` and `docs/bench/cuda_*.png` currently hold the
CPU-and-Python-only sweep from the M3. `cuda_stages.png` is absent because there
are no CUDA rows yet; that is correct, not a failure.

## 2026-08-10 — First session on the Windows target box: pool + Python gate PASS, toolchain absent

- Machine: Windows 11 Pro (10.0.26200), RTX 4070 Laptop GPU 8 GB, driver 592.82,
  Python 3.14.3. Pool: all three months extracted at the repo root.
- **The CUDA gap stays open — the box cannot compile.** VS 2022 Community is
  installed *without* the C++ workload (no MSVC toolset, no vcvars), there is no
  CUDA Toolkit (driver only) and no system CMake. All three need elevation this
  session did not have. `scripts/setup_windows_toolchain.ps1` (new) adds the C++
  workload to the existing VS and the CUDA Toolkit via winget; CMake 4.4.2 +
  ninja are already in the repo venv via pip. After it runs once, the three
  README commands close the pending-numbers section above.

### Pool integrity + Python side of the correctness gate — PASS

- Per-file differential `oracle.extract` vs `clean_vectorized.extract`, all nine
  tuple fields, on days 01, 02, 16, 17 (the counter-reset window): **bitwise
  identical** — 765,711 / 998,920 / 1,109,468 / 858,651 events. Per-file event
  counts agree between the two arches on all 28 day-files; total **21,872,663**
  events, the exact count every earlier entry measured.
- Union oracle on prefixes: 1 day = 765,711; 7 = 3,900,837; 28 = **14,372,237**
  — all three exact. The reset signature reproduces (day 17 adds 15 new
  (head, cap_seq) keys out of 858,651 events), so this box's copy of the pool is
  equivalent to the one the M3 sweeps measured.
- Indicative clean-mode timings on this box (not results_cuda rows — spec §6.5
  says that file is regenerated whole, one machine, once CUDA rows can join):
  py-naive 1d median-of-3 2.645 s; py-numpy 1d 3.134 s, 7d 20.7 s, 28d 89.2 s.

### Two Windows-only defects found and fixed

- **Python suite 229 passed, 5 skipped** (real-data analytics tests skip while
  `events_3mo.duckdb` is absent) — after fixing two failures no macOS run could
  see: `test_render.py` read the UTF-8 golden fixture and the written report
  back with the locale codec, which on Windows is cp1252, so every em-dash
  compared as mojibake. All report read-backs in tests now pass
  `encoding="utf-8"`, and `tests/regen_golden.py` writes the golden as UTF-8
  (regenerating it on Windows would have produced a cp1252 fixture).
- **The §4 CRLF risk happened.** This clone has `core.autocrlf=true` and was
  made from `main`, which predates the branch's `.gitattributes`: switching
  branches rewrites only files that differ, so the tracked `*.sh` scripts sat
  CRLF in the working tree (bash would die on `$'\r'`). Re-checked out with
  attributes → LF. Committed CSVs were unaffected (all changed on-branch, so
  the eol=lf rule applied at switch).

### Static review of the never-compiled path (no blocking defect)

`CudaCleaner.cu` + `cuda_clean_main.cpp` against `CapEventExtractorFlat`, and
`platform_metrics.hpp` on its `_WIN32` branch, reviewed line by line. Recorded
for the record, none blocking on this pool: the GPU parser diverges from
`load_columns` only on rows the CPU would skip or roll back (short rows,
unparsable cells — zero exist in the pool, and `--verify` is the bitwise gate);
the CUB `DeviceSelect` calls take `int` item counts, capping a single file at
2 GB (day-files are 58 MB); GPU `clean_s` sums the seven stage timers and so
excludes host-side event materialization; `provenance()` records
platform/python/gpu/nvcc but not the §6.5 compiler-id/cores/RAM extras.

## 2026-08-10 — CUDA numbers closed: first compile, first GPU run, full sweep on the target box

Same box as the entry above, later the same day, after the user ran
`scripts/setup_windows_toolchain.ps1`: MSVC 14.41 (VS 2022 Community) +
CUDA Toolkit 13.3 (V13.3.73), CMake 4.4.2 from the repo venv. The script took
three iterations against the real installer — setup.exe hands off and returns
immediately (fixed by polling for the toolset), this installer version rejects
`--wait` with exit 87, and PowerShell 5.1's `Start-Process` splits unquoted
spaced paths (`--installPath C:\Program`). All three fixes are in the script.

### First MSVC/CUDA compile — spec R3 predicted "a one-line fix"; it took three

1. CUDA 13's CCCL refuses cl.exe's traditional preprocessor (fatal C1189) →
   `-Xcompiler=/Zc:preprocessor` on the `.cu` compilation.
2. CCCL 3.0 removed `cub::CountingInputIterator` → `thrust::counting_iterator`.
3. `platform_metrics.hpp` included `windows.h` without `NOMINMAX`; the min/max
   macros broke the first TU calling `std::min` after it (`cuda_clean_main`).

Everything else — the whole monolith + DuckDB + store stack — compiled clean on
MSVC at first try. The Windows DuckDB v1.2.2 asset downloaded, worked, and its
SHA256 is now pinned (closing the "fill in after first verified download" note).
C++ suites from the repo root: bench-only **34/34**, full **50/50**, both
including the two real-data tests. R9 (Windows DuckDB asset misbehaving) never
triggered.

### `--verify` earned its keep twice

- **GPU parse, real defect.** First `--verify` run failed at event 25,194 of
  day 1: GPU torque one ulp under CPU on a cell reading `2.0020000000000002`.
  The §4 "plain decimal, ≤ 3 dp" premise is false on the real pool (see the
  correction note in the spec): 66,553 AppTorque cells on day 1 (~2%, zero in
  Count/Status) carry full 17-digit double reprs, whose mantissas exceed 2^53
  and double-round through the kernel's integer-mantissa-then-divide path. A
  ucrtbase probe confirmed MSVC's strtod correctly rounded — the CPU was right,
  the GPU wrong. Fix: the kernel flags rows whose mantissa exceeds 2^53
  (Count-block hits are fatal by design; none exist), and the host re-reads
  flagged events' torque/status from the raw line with strtod. Post-fix:
  `--verify` bitwise-green on days 01, 02, 16 — 2,874,099 events.
- **Driver, real defect.** The sweep's cross-arch gate then killed the run at
  the 7-day volume: `cuda=765711` vs `3901017` everywhere else. Not a GPU bug —
  with `--verify` the binary prints per-file `verify ok: … (N events)` lines
  before its summary, and the driver's `_EVENTS.search` took the *first* "N
  events" in the blob (day 1's), passing at one day by coincidence. The driver
  now takes the last match; every contender prints its summary last.

### Full sweep — 68 measured rows + 2 extrapolated, every count oracle-exact

`python bench/run_bench_cuda.py --data telemetry_…_2026-02` (extracted dir).
Every arch at every volume × repeat emitted identical event counts: 765,711 /
3,901,017 / 21,872,663 — the cross-arch gate held for all 9 volume×repeat
cells. `clean_s` medians of 3 (seconds):

| arch [mode] | 1 day | 7 days | 28 days |
|---|---:|---:|---:|
| py-naive [clean] | 2.686 | (18.8) | (75.2) — extrapolated |
| py-numpy [clean] | 3.142 | 19.694 | 91.003 |
| cpp-1T [clean] | 1.641 | 10.689 | 46.772 |
| cpp-MT 8T [clean] | 1.666 | 2.014 | 7.677 |
| **cuda [clean]** | **0.059** | **0.377** | **1.821** |
| mono-1T [clean, no-store] | 1.646 | 10.754 | 46.510 |
| mono-1T [e2e] | 8.334 | 44.433 | 230.449 |
| mono-MT 8T [e2e] | 8.560 | 11.854 | 42.064 |

- **Headline: CUDA cleans the month in 1.82 s — 12.0 M events/s — 25.7× the
  single-thread C++, 4.2× the 8-thread C++, 50× vectorized Python.** The stage
  breakdown says the transform has stopped being the cost: at 28 days
  (hot cache) disk read is 1.17 s of the 1.82, and the four GPU compute stages
  (index+parse+delta+compact) total ~0.29 s for 2.4 GB / 21.9 M events.
- The `e2e` truth is unchanged: the single-writer DuckDB store is 79.8% of
  mono-1T's month wall-clock (230.4 s vs 46.5 s clean). 8-thread e2e lands at
  42.1 s — per-thread stores + merge parallelize the write far better here
  (5.5×) than on the M3's numbers.
- py-naive beats py-numpy here too (2.686 vs 3.142 at 1 day) — the M3 finding
  about round_trip parse + tuple materialization replicates on Windows.
- Caveats: laptop thermals; cuda 28-day rep 1 measured 3.887 s against 1.8 s
  for reps 2-3 (cold file cache on the first pass, and rep 1 also carries
  `--verify`'s CPU-side load in wall time though not in `clean_s`); `cpp-MT` at
  1 day equals `cpp-1T` (one file, file-grain threading); the `# nvcc` line in
  the CSV header was corrected by hand after the run — the sweep ran from a
  shell predating the CUDA install, so PATH had no `nvcc` (value taken from
  `nvcc --version` on the same box; `provenance()` now falls back to
  `CUDA_PATH` so this cannot recur).
- Outputs: `bench/results_cuda.csv`, `bench/results_cuda_stages.csv`, and
  `docs/bench/cuda_{throughput,scaling,stages}.png` — the stages plot exists
  for the first time.

Remaining gaps, deliberate: the 5 real-data Python analytics tests still skip
(`events_3mo.duckdb` not built on this box — `scripts/build_store.sh`, ~5 min,
any time it is wanted); the ZeroMQ runtime stays off on Windows by design
(spec §2 non-goal).

## 2026-08-13 — Review of the CUDA branch: the CUDA clean window was smaller than everyone else's

A line-by-line review of `feat/cuda-cleaning-bench` (35 files, +6k lines)
against the tree and the raw CSVs. Three findings correct entries above; the
rest are closed in code on the branch. The numbers below were established on
the M3 (no GPU), so everything GPU-timed awaits the re-run.

### The headline correction: `clean_s` for CUDA measured less work

Supersedes the CUDA numbers in the 2026-08-10 "CUDA numbers closed" entry, and
the ratios derived from them. The seven stage timers all stop before the host
loop that materializes the `CapEvent` vector — the "materialize events in
memory" half of spec §6.1's definition of `clean` mode — and before
`check_header`, the 58 MB `cudaHostAlloc` and every `cudaMalloc`.
`cuda_clean_main` summed exactly those seven timers; every other contender
times its whole per-file work.

Two independent measurements put the untimed part at roughly the size of the
timed one:

- A host-side replica of the materialize loop (same per-event `std::string`
  timestamp, same reserve+push_back) on the M3: 0.059 s at 765,711 events,
  1.775 s at 21,872,663 — against recorded CUDA `clean_s` of 0.059 s and
  1.821 s at the same volumes.
- The recorded 28-day CUDA row itself: `cpu_pct=469%` on a 1.805 s window is
  8.5 s of process CPU; the cpp-1T row alongside reads 98%.

So the published clean-phase ratios halve, roughly: 4.2x over cpp-MT → ~2x,
25.7x over cpp-1T → ~13x. The end-to-end conclusion survives almost unmoved
(~1.24x → ~1.23x over mono-1T), because persistence dominates — the branch's
own finding, which never depended on the flattered number.

Fixed on the branch: an eighth `materialize_s` stage (host clock — it is CPU
work), included in `clean_s` and in the stages line; the driver records the
process wall clock as `total_s` for the cuda and cpp rows as it always did for
mono; `docs/bench/results.md` re-states the tables as median [min–max] with
the window correction marked as an estimate until the re-run.

### The GPU parser was the only contender that fabricated data on short rows

`parse_rows` indexed rows by newline only; a row whose fields end early parsed
every remaining field from an empty range, and `parse_num` returns 0.0 for an
empty range without raising `inexact`. A truncated row therefore became a row
of zeros: a fabricated reset against the previous row and a fabricated
aggregated event against the next, silently, where `CsvRawReader`,
`CapEventExtractorFlat`'s loader and `oracle.py` all skip the row. The pool is
clean, so nothing bites today; the kernel now counts columns per row and the
host refuses the file on a mismatch, the same treatment as the 2^53 Count
case. `clean_vectorized.py` had the mirror-image defect (pandas raises on a
malformed cell, NaN-pads a short row) and now delegates dirty input to
`oracle.extract`; two differential tests pin both cases.

### Corrections to entries above

- **"bench-only 34/34, full 50/50"** (2026-08-10 entry): not reproducible from
  the tree — no CMake configuration of this branch yields 34 or 50. Measured
  here today: bench-only is **37 tests** (35 pass + 2 that skip without the
  pool; 37/37 with it). The full-build counts on Windows should be re-recorded
  by the re-run.
- **"28 day-files is roughly two hours of interpreted loop"** (pre-sweep CUDA
  entry, and spec §6.3): the branch's own 1-day measurement says 2.686 s, so
  the month is ~75 s per repeat — two orders of magnitude off. The
  extrapolation machinery this estimate justified is removed; `py-naive` now
  runs measured at every volume, and the two extrapolated rows in the
  committed CSV are filtered out of the plots until the re-run replaces the
  file.
- **Line endings** (spec §4 said LF): the pool is CRLF on every row, header
  included, inside the zips — 86,400 CR against 86,400 LF per day-file. The
  trailing-`\r` strip in the two new parsers is load-bearing, not defensive;
  `CsvRawReader` survives via `stod` stopping at the `\r`. Spec corrected in
  place with a dated note.

### What must be re-measured on the RTX box

One sweep, corrected timers: CUDA rows with `materialize_s` included and wall
`total_s`; `merge_s` recorded for mono-MT (the driver used to hardcode 0.000
— the 28-day mono-MT merge is ~150 s of measured work that read as zero);
py-naive measured at 7 and 28 day-files; the full-build C++ suite count. Until
then, every GPU number above is a shape, not a measurement — the same reading
the 2026-08-13 M2 entry already established for the parallel CPU rows.

## 2026-08-13 — Re-run on the RTX box with the corrected timers: the estimates become measurements

Supersedes the numeric estimates in the "Review of the CUDA branch" entry
above (~2x / ~13x / ~1.23x, and its "roughly half" reading of the recorded
CUDA rows); the review's finding about *what* was mistimed stands. Same box
as 2026-08-10: RTX 4070 Laptop 8 GB (driver 592.82), MSVC 14.41, CUDA 13.3,
mains power, fans free, ordinary desktop background load. Branch tip
`622c4d2`; `bench/run_bench_cuda.py`, volumes 1/7/28 × 3 repeats.

Operational note, recorded because it cost one sweep: the first run died
mid-flight when a concurrent session on the same machine checked out a
different branch under it (`main` does not carry
`python/clean_vectorized.py`, so the py-numpy contender vanished from disk
between two subprocess launches). Everything below comes from a second, clean
run executed in a dedicated git worktree of `feat/cuda-cleaning-bench`, with
binaries rebuilt and all three suites re-run inside that worktree while the
main checkout stayed with the other session.

Suite counts on the worktree that produced the swept binaries — the counts
the review entry said must replace the unreproducible "34/34, full 50/50":
bench-only **37/37 pass** (pool present, via junction next to the binary);
full build with ZMQ off **62/62 pass**; Python **235 passed + 5 skipped =
240 collected**.

Gates, all green: `--verify` passed at repeat 1 — its CPU differential is
visible as wall, not clean (repeat-1 CUDA `total_s` 58.9 s against 8.4 s
`clean_s`, repeats 2–3 ~8.3 s total); event counts identical across every
arch at every volume (765,711 / 3,901,017 / 21,872,663) and every e2e
configuration landed exactly on the oracle_union counts; `materialize_s`
populated in the stages CSV; mono-MT `merge_s` > 0; no `extrapolated` rows.
One expected asymmetry, recorded so the next reader does not chase it:
`bench_cpu`'s `total_s` equals its `clean_s` to the millisecond in every row.
That is two clocks over the same span — the store-free binary does nothing
outside its clean loop — not a missing wall measurement (its `metrics:` line
parses; rss and cpu% are populated).

28-day medians [min–max], clean mode, corrected timers:

| arch | clean_s | vs cuda |
|---|---:|---:|
| cuda | 6.43 [6.33–8.34] | — |
| cpp-MT 8T | 8.21 [8.12–8.32] | 1.3x |
| cpp-1T | 46.26 [45.91–46.57] | 7.2x |
| py-naive | 74.64 [74.32–76.31] | 11.6x |
| py-numpy | 85.82 [84.53–86.99] | 13.4x |

The correction is larger than the review estimated. Materialize alone is
4.64 s at 28 days — 72% of the corrected clean, not "roughly half" — while
the seven original stages still sum to 1.80 s: the old 1.82 s recording was
accurate for what it measured, and it measured about a quarter of the phase.
The M3 host-side replica (1.775 s) underestimated this box's materialize
loop by 2.6x — MSVC's allocator and the per-event string build price the
same code differently, which is exactly why the entry above refused to let
the replica stand in for the measurement. Ratio corrections, estimate →
measured: cpp-MT ~2x → 1.3x; cpp-1T ~13x → 7.2x; py-naive "~75 s per 28-day
repeat" → 74.6 s [74.3–76.3] (that one held).

End to end at 28 days: mono-1T 257.6 s [254.5–266.6] against 45.5 s
store-free clean → persistence 212.1 s, 82% of wall. cuda clean + store =
218.5 s → **1.18x** vs mono-1T (review estimate ~1.23x); cpp-MT 220.3 s →
1.17x; CUDA over cpp-MT end to end ~1.01x. mono-MT e2e 108.4 s [107.1–111.6]
with `merge_s` **58.7 s [57.7–62.5] measured** — the hardcoded 0.000 is gone,
and the "~150 s" this entry's predecessor guessed for the hidden merge was
itself off by 2.6x. Guesses go in brackets; sweeps get re-run.

CUDA stage medians at 28 days (s): read 1.199, h2d 0.179, index 0.060,
parse 0.165, delta 0.021, compact 0.041, d2h 0.137, materialize 4.636. GPU
compute is 0.287 s — the pre-correction "~0.29 s" reading of the kernels held
exactly. Context and allocations stay outside `clean_s` and inside the wall
(8.43 s median total against 6.43 clean), which is where spec §6.1 puts them.
`docs/bench/results.md`, the README benchmark table, the roadmap line and the
three `cuda_*.png` plots now carry these measured numbers.

## 2026-08-13 — Resweep on actively-cooled hardware: the ratios are settled, and the harness had one more thumb on the scale

**HEAD:** `ba6d4f8` (branch `bench/resweep-on-cooled-hardware`). **Hardware:**
HP Victus 16-r0xxx — Intel i7-13700H, 6 P-cores + 8 E-cores (20 threads),
16 GB, NVMe SSD, dual-fan **active cooling**, on AC power, Windows 11
("Balanced" plan), machine otherwise idle. **Toolchain:** MSVC 19.41 Release
(`/O2 /Ob2 /DNDEBUG`), DuckDB v1.2.2 official `windows-amd64` binary, libzmq
4.3.5 from source — the first ZMQ build this repo has done on Windows;
`ctest` **85/85** before the sweep, on the first run. **Command:**
`BUILD_DIR=build-sweep/Release bash bench/run_bench.sh` under Git Bash, timed
by `bench/win_time.cpp` (QPC + `GetProcessTimes` + `PeakWorkingSetSize`,
printed in the BSD `time -l` shape `parse_time()` already reads), because
`/usr/bin/time` does not exist on Windows — the portability defect the resweep
prompt flagged, now closed for macOS + Windows and a loud abort elsewhere.

**Runs: 81 of 81, one uninterrupted session.** Binaries built before the run
and untouched during it; no SIGTERM splice, no re-measured block — the first
sweep in this log that carries neither caveat. **Correctness gate: every run
exact.** 21,872,663 distinct `(head_id, ts)` events at 28 days, 3,901,017 at
7, 765,711 at 1 — all three repeats of all nine configurations per volume,
against the independent Python oracle.

### Found on the way in: mono and MAS were not writing the same rows

The smoke run surfaced it before the sweep could: a MAS worker cleaned a
day-file in ~10 s of wall while `mas_monolith` spent 18.4 s of pure CPU on the
same file — half the cycles for "the same work" means it was not the same
work. `run_bench.sh` never passed a machine id to `mas_worker`, whose argv
default is `MCC`; the monolith and `mas_merge` got the full 35-character id.
That id is the first column of `UNIQUE(machine_id, head_id, ts)` and is
written 21.9M times per month: 3 chars stay inside DuckDB's inline string
representation, 35 go out of line, and on this build that is the difference
between 9.1 s and 18.3 s for `clean.exe` on one day-file with nothing else
changed. On the M2 the asymmetry was timing-neutral (v=1 mono and MAS clean
within noise, long id and short), so no Mac figure is retroactively tainted —
but here it would have handed MAS a 2× head start on the clean phase, and the
sweep would have measured a default argument. Fixed in `ba6d4f8` before any
measured row: workers now write the real id everywhere. Same defect class as
`638478b` — two configurations timed doing different work.

### 28-day medians (3 repeats each)

| config | clean_s | merge_s | total_s | spread (total) | vs mono-1T |
|---|---:|---:|---:|---:|---:|
| mono-1T | 537.79 | — | 537.79 | 1.1% | 1.00x |
| mono-MT T=2 | 264.91 | 72.18 | 338.34 | 1.5% | 1.59x |
| mono-MT T=4 | 134.91 | 69.67 | 204.58 | 0.3% | 2.63x |
| mono-MT T=8 | 86.04 | 70.59 | 157.35 | 1.1% | 3.42x |
| mas N=1 | 514.59 | 430.40 | 944.32 | 0.6% | 0.57x |
| mas N=2 | 279.52 | 69.08 | 348.14 | 0.3% | 1.54x |
| mas N=4 | 151.48 | 68.62 | 219.83 | 0.4% | 2.45x |
| mas N=8 | 111.88 | 70.81 | 182.59 | 1.5% | 2.95x |
| mas N=16 | 74.90 | 64.85 | 140.44 | 4.8% | **3.83x** |

### What this settles

- **Repeatability.** `clean_s` spreads 0.1–1.8% across every 28-day
  configuration; `total_s` 0.3–4.8% (worst: MAS N=16). The same harness on the
  M2 spread 21% (mono-MT) and 53% (MAS) in the interleaved A/B. On this
  machine a number records the code, not the run order.
- **The end-to-end ratio is a measurement, not a bound: 3.83x at N=16**
  (537.8 s → 140.4 s), clean phase alone 7.2x across 16 workers on 20 hardware
  threads. This replaces 1.84x-with-tens-of-percent-uncertainty as the repo's
  reference number. It does not "correct" 1.84x: different machine, different
  cost mix — the M2 spent 70% of MAS's wall in the merge, this box 46%,
  because the per-row clean path costs ~5x more here while the set-based merge
  costs the same (64.8 s vs the M2's 64.0 s — DuckDB's internal pass is close
  to platform-neutral; the per-row application code is not).
- **The mono-MT/MAS gap has an owner: parallelism, not process isolation.** At
  equal N the thread pool wins — MAS N=8 trails mono-MT T=8 by 25.2 s (182.59
  vs 157.35) — and MAS takes the matrix top only because it is swept to N=16
  (140.44, 16.9 s ahead of T=8, well clear of the 1.1–4.8% spreads). The PR #9
  sweep's 20.9 s "MAS ahead at same-ish parallelism" was the merge-clock
  artifact (`638478b`) plus two thermal states; the A/B called the tie
  correctly on the M2, and this measurement gives the sign a mechanism.
- **Merge cost is flat across N≥2 on a second platform** (69.1 / 68.6 / 70.8 /
  64.8 s for N=2..16; 72.2 / 69.7 / 70.6 s for T=2..8): `merge_all` costs what
  the volume costs. The N=1 control, which falls back to per-row `merge_from`,
  pays **430.4 s — 6.2x the set-based pass** over the same total volume (the
  same fallback was ~10% over set-based on the M2). Per-row index probing is
  precisely the work this platform taxes, which is the strongest evidence yet
  for the set-based design.
- **Absolute seconds do not transfer between machines.** mono-1T's month:
  537.8 s here, ~101–108 s on the M2, same source, both Release. Shape
  transfers; seconds do not. Anyone quoting this repo's performance quotes a
  machine.

### Supersession

The reference numbers for every end-to-end ratio and the mono-MT/MAS
comparison are now this entry's table; `docs/bench/results.md` (table +
analysis) and the README's benchmarking section were rewritten from this
sweep, and the four plots regenerated from the new `bench/results.csv`. The
2026-08-11 merge_all projection paragraph is marked superseded in place; the
2026-08-13 interleaved A/B above stands as the M2 measurement that made this
resweep necessary, and its "until then" closes here. The M2's like-for-like
merge A/B (65.9 → 22.8 s, 2.89x) remains valid as measured.
