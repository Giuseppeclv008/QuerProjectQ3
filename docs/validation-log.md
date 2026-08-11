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

## 2026-07-24 — Plan 6: Analytics foundation, WP2 toolkit on the real three months (real data)

- HEAD at run: 654c491 (branch `feat/agentic-analytics`). Store: `scripts/build_store.sh events_3mo.duckdb` over all three month zips → `monolith: 89 files, 55132433 events, clean 76.154 s, merge 133.593 s, total 209.746 s, store holds 20347822 rows` (369 MB). The 55.1M→20.3M gap is the counter-reset dedup the Plan 5 entry above characterised (UNIQUE(machine_id, head_id, cap_seq) collapses replays identically for every architecture).
- Python suite (project venv): **85 passed** — 80 fixture tests + 5 real-data tests (`python/tests/test_real_data.py`, skipped when the store is absent). C++ suite unchanged this plan (Task 1 semantics fix landed with its own tests earlier on the branch).

### Independent oracle cross-check (spec §10): toolkit SQL == raw-CSV re-derivation — PASS

- `python/oracle_kpi.py` recomputes the headline counts straight from the raw CSV — no DuckDB, no toolkit SQL, no shared code — the same discipline that caught the Plan 5 distribution defect. On `2026-02-01.csv` it reproduces the design-time figures **exactly**: `successful=427643, failed=4, no_load_cycles=337772, capping_operations=427802 (=427643+155+4)`. The month's toolkit counts contain the day's (`overview` caps/no-load ≥ the oracle day), tying the SQL to the independent oracle. The Task-1 status-semantics fix is therefore correct on real data, not just on fixtures.

### Headline KPIs, `overview()` / `success_rates()` / `torque_stats()` / `capping_speed()`

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

- **anomalies** (band [1.5, 2.5], k=3): 2026-02 `{faults: 371, threshold_hits: 68, deviation_hits: 678325}`; 3-month `{faults: 585, threshold_hits: 129, deviation_hits: 1734460}`. Faults == the status-65 count (consistent). Threshold hits == `invalid_torque` (both are the out-of-band caps). **Caveat:** robust MAD deviation flags ~10% of caps (678k/6.67M) — the successful-torque spread is extremely tight (σ≈0.016 Nm), so median±3·MAD is a narrow band and normal process jitter clears it. This is the detector behaving as specified on a very-low-variance signal, not a defect; a real deployment would widen `mad_k` or gate deviation on a minimum absolute delta. Noted for Plan 7 report-agent tuning.
- **trend / drift** (Mann-Kendall, |τ|≥0.5, daily torque): **0 heads drift** over 2026-02 and over all three months. Expected for a machine holding 2.0 Nm to ±0.016: day-to-day mean torque is essentially flat, so no monotone walk. The tool ran clean across the 3-month range (`status ok`, 36 drift entries), which is the assertion the real-data test pins.
- **idle_periods** (min 300 s): 2026-02 → 12,276 periods, 26,949,710 s total; 3-month → 21,802 periods, 137,465,461 s total. Sustained no-load runs are common, consistent with 337k no-load cycles/day.

### Validation-process notes (two plan-code fixes needed to run real data)

- `scripts/build_store.sh` assumed every month zip extracts into a `telemetry_.../` subfolder; the Mar/Apr zips drop day-files flat into the cwd, so the first run silently built February only (28 files). Fixed to collect day-files whether flat or in a subdir (verified: 89 files across 02/03/04).
- `python/tests/test_real_data.py` looked for the store at `events_3mo.duckdb` (i.e. under `python/`), but `build_store.sh` and the plan's own record-numbers script put it in the repo root; the tests silently skipped. Fixed to `../events_3mo.duckdb`.
- Spec reconciled (§5.4, §12 OQ#2 RESOLVED): capping speed is SQL in `capping_speed()`, extractor unchanged, no schema column, no reprocessing.

## 2026-07-26 — Plan 7: WP3 report agent and WP4 CLI, end-to-end on the real three months

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

### Still open

- **The three committed reports in `docs/reports/` were generated from the old
  store and every number in them is computed on the residue.** They must be
  regenerated after `scripts/build_store.sh` rebuilds `events_3mo.duckdb` under
  the new key. Expect February's `rows scanned` to rise from 10,450,551 toward
  ~21.9M, and the derived KPIs to move with it.
- Throughput in those reports is also wrong for a second reason: `capping_speed`
  divided a day's closures by a flat 24 h. It now divides by the hours that
  actually saw a closure. On the golden fixture that is 0.25 → 6 pieces/hour.
