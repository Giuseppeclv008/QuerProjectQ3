# Resilience: Heartbeats & Work Re-dispatch — Design Spec

Date: 2026-07-08
Status: approved
Parent spec: `2026-07-04-iiot-data-refinement-mas-design.md` (§5.2, §10)
Predecessor: Plan 3 (`2026-07-07-zeromq-agent-runtime.md`) — deferred this work explicitly.

## 1. Context & Problem

Plan 3 delivered a working ventilator/worker/sink pipeline over ZeroMQ PUSH/PULL,
validated end-to-end on real data. Its liveness posture is deliberately thin:

- The coordinator counts stragglers as failed after 60 s of sink silence and moves on;
  work owned by a dead worker is *lost*, never re-dispatched (spec §10 calls for
  re-dispatch on missed heartbeat).
- A worker blocks forever on `recv` if its STOP never arrives (documented limitation
  in `worker_main.cpp`), and blocks forever PUSHing a result if the coordinator
  already exited.

This plan closes both gaps: dead workers are detected via heartbeats and their work
re-dispatched to survivors; workers self-terminate when the coordinator vanishes.

Two facts discovered during design shape everything below:

1. **Anonymous PUSH/PULL pre-queues invisibly.** The coordinator PUSHes all items up
   front; ZeroMQ round-robins them into per-worker queues immediately. A killed
   worker therefore loses not just its in-flight item but an unknowable set of queued
   items. No worker can report what sits in its ZMQ buffer — targeted "re-dispatch
   the one item it was chewing" designs are structurally leaky here.
2. **A killed worker's store may be unusable.** `kill -9` mid-write can leave a
   DuckDB file that violates `merge_from`'s closed/checkpointed precondition. Items
   the dead worker *already completed and reported* may therefore live only in an
   unmergeable file.

## 2. Goals & Non-Goals

**Goals**

1. Detect worker death via heartbeats; re-dispatch affected work; a run with a
   mid-run worker kill completes with counts equal to the oracle.
2. No process ever hangs: coordinator always terminates; workers self-exit when the
   coordinator disappears (closes both Plan 3 limitations).
3. Poison-item protection: an input that crashes workers cannot serially destroy the
   pool or prevent run completion.

**Non-Goals (deferred, unchanged owners)**

- Coordinator crash recovery / dispatch journal (spec accepts Docker restart +
  idempotent rerun).
- Registration/config REQ/REP, PUB/SUB fan-out (spec §5.2 remainder).
- Docker/compose packaging, KPI/anomaly agents, benchmark harness.

## 3. Approach Decision

**Chosen — A: heartbeat channel + outstanding-set re-dispatch.** A third PUSH/PULL
channel carries heartbeats; the coordinator tracks the outstanding-item set and which
worker produced each result. On a death it re-dispatches *all* outstanding items plus
the dead worker's completions, and writes the dead worker's store off. Correctness
rests on the idempotency backbone (UNIQUE `(machine_id, head_id, cap_seq)` upsert;
idempotent `merge_from`) — duplicates from partial processing, re-dispatch overlap,
or merging an intact "written-off" store are all absorbed. Smallest correct diff;
keeps the validated transport; reuses the existing ZMQ adapters unchanged.

**Rejected — B: targeted re-dispatch** (re-dispatch only the item named in the last
heartbeat): loses ZMQ-queued items at the dead worker (fact 1); converges to A once
patched. **Rejected — C: ROUTER/DEALER (Paranoid Pirate)**: precise per-worker
assignment, no phantom queues, but replaces the validated ventilator, needs new
adapters and manual load-balancing — overkill at this scale.

## 4. Topology

```
worker  PUSH ──► PULL  coordinator   heartbeats (new channel)
worker  PULL ◄── PUSH  coordinator   work items (unchanged)
worker  PUSH ──► PULL  coordinator   results    (unchanged)
```

Coordinator binds all three endpoints; workers connect. No new socket types:
`ZmqPushSink` / `ZmqPullSource` are reused for the heartbeat channel. The DIP
boundary is unchanged — domain code sees only `IMessageSource` / `IMessageSink`.

## 5. Protocol

- **Heartbeat message**: `worker_id` + monotonically increasing `seq`. Codec gains
  `encode(Heartbeat)` / `decode_heartbeat` in the existing tag-line format, with the
  existing strict-parse conventions.
- **`WorkResult` gains `worker_id`** so the coordinator can attribute completions
  (needed for the write-off rule). Internal protocol; both ends ship together; no
  backward compatibility required.
- **`worker_id` is a mandatory CLI argument** to `mas_worker` — explicit and
  deterministic in tests and demo scripts; no generation logic.
- **Heartbeat cadence — no worker thread.** The worker stays single-threaded. Its
  wait for work is a loop of short-timeout `recv` calls (1 s tick); every empty tick
  sends one heartbeat, so an idle worker is never silent. One more heartbeat follows
  each result, and a result arriving on the results channel also refreshes liveness
  on the coordinator. The only silent window is while processing one file, so the
  death threshold must exceed the worst-case per-file time: default **30 s**
  (observed per-file cost 2–7 s; see §9). Trade accepted: up to ~30 s detection
  latency instead of ~3 s, in exchange for zero threading.

## 6. Coordinator

`run_coordinator` becomes a tick loop. State:

- `outstanding`: item → dispatch count (items dispatched, no accepted result yet).
- `registry`: `worker_id` → last-seen time, set of completed items, alive/dead.

A time source (`std::function<steady_clock::time_point()>`) is injected so unit
tests drive the clock without sleeping.

Each tick (results `recv` with ~200 ms timeout; heartbeat source drained
non-blocking):

- **Result received** → refresh `last_seen`, record attribution, remove item from
  `outstanding`. A result for an item no longer outstanding (re-dispatch race) is
  logged and dropped — first result wins.
- **Heartbeat received** → refresh `last_seen`; first heartbeat registers the worker.
  Heartbeats from tombstoned (declared-dead) workers are ignored — dead is dead.
- **Deadline sweep** → any registered worker silent past the threshold is declared
  dead (tombstoned). Re-dispatch set = every outstanding item ∪ the dead worker's
  completions. Each re-dispatched item's dispatch count increments; an item
  exceeding the cap (**2 re-dispatches**) is marked permanently failed and leaves
  `outstanding`.
- **Abort conditions** → zero live workers with `outstanding` non-empty; or no
  heartbeat from anyone within the threshold of run start. Either way: remaining
  items counted failed, loud stderr, exit non-zero. (The existing 60 s send-side
  timeout on the work socket remains as the mute-socket backstop.)
- **Termination** → `outstanding` empty (every item ok or permanently failed) →
  send STOP × currently-live workers per the registry. The blind `num_workers` CLI
  argument is dropped; never-registered stragglers self-exit via their idle timeout.

The coordinator logs joins, deaths, re-dispatches, and drops to stderr — the chaos
test asserts on these lines.

The dead worker's `.duckdb` is written off: its contents are re-created in survivor
stores via re-dispatch. If the file happens to be intact and gets merged anyway, the
idempotent upsert absorbs the duplicates — harmless either way.

## 7. Worker

Small, surgical changes:

- Work `ZmqPullSource` gets a **1 s recv timeout** — the wait-loop tick. The worker
  counts consecutive empty ticks: each one sends a heartbeat; **60 in a row** means
  the coordinator is gone or done → the loop exits. Closes the lost-STOP infinite
  hang, and needs no clock — tick counting is unit-testable with fakes.
- Results and heartbeat `ZmqPushSink`s get the existing 60 s send timeout → a dead
  coordinator turns a would-be infinite block into a thrown `std::runtime_error` →
  caught in `main` → exit 1. Closes the orphaned-PUSH hang.
- `CleaningWorker` ctor gains `worker_id` and a heartbeat sink reference; emits a
  heartbeat on every empty wait tick and after each result; stamps `worker_id` into
  every `WorkResult`.
- `mas_merge`: an input store that fails to ATTACH is warned about and skipped
  (today the whole merge aborts) — tolerates crashed-worker files.

CLI shapes:

```
mas_worker      <work_ep> <result_ep> <hb_ep> <out.duckdb> <worker_id> [machine_id]
mas_coordinator <work_ep> <result_ep> <hb_ep> <day1.csv> [day2.csv ...]
```

## 8. Failure Semantics (edge cases)

| Scenario | Behavior | Why it stays correct |
|---|---|---|
| Worker dies mid-file | Item re-dispatched; partial rows in dead store | Partial ⊂ survivor's full reprocess; upsert |
| Worker dies after reporting results | Its completions re-dispatched; store written off | Survivor stores hold a superset |
| False-positive death (slow file) | Zombie's late results dropped; no STOP for it; self-exits on idle timeout | Waste, never incorrectness; 30 s ≫ 7 s makes it rare |
| Zombie heartbeat after tombstone | Ignored — no revival | Simplicity; zombie self-exits |
| Zombie steals a STOP or a re-dispatched item | Delivery is count-accurate, not target-accurate (anonymous round-robin PUSH) | Waste, never incorrectness — bounded by the idle-exit → death-sweep → abort/cap cascade |
| Poison item (crashes any worker) | Re-dispatch cap 2 → permanent fail; run completes, exit 1 | Pool survives; failure is loud |
| All workers die | Abort with summary, exit 1 | Tick loop + timeouts: coordinator cannot hang |
| Coordinator dies | Every worker exits within ~60 s (idle recv timeout or send-timeout throw) | No orphan processes |
| Malformed heartbeat/result | Log + ignore (existing decode-failure pattern) | Never crashes the loop |

On long skewed runs a starved-but-alive worker can exceed its 60-tick idle
budget mid-run and self-exit, appearing to the coordinator as a phantom
death; counts stay exact (its items re-dispatch), only the `workers_died`
stat inflates.

## 9. Defaults

| Knob | Default | Rationale |
|---|---|---|
| Worker wait tick (work recv timeout) | 1 s | Heartbeat per empty tick; idle worker never silent |
| Death threshold | 30 s | > worst observed per-file time (7 s) with margin |
| Coordinator tick (results recv timeout) | 200 ms | Responsive sweeps without busy-spin |
| Re-dispatch cap | 2 per item | One honest retry + one bad-luck retry |
| Worker idle exit | 60 consecutive empty ticks (~60 s) | Generous; STOP normally arrives in ms |
| Send timeouts (all PUSH sockets) | 60 s | Matches existing coordinator setting |
| Worker sink linger (results + heartbeats) | 0 ms | Discovered during chaos validation: Plan 3 coupled ZMQ_LINGER to the send timeout, so a worker with queued beats to a dead coordinator exited its loop on time but the process hung ~60 s more in context teardown, breaking §8's ≤ ~60 s promise. Zero linger is protocol-safe: the coordinator holds every result before it sends STOP, and heartbeats are fire-and-forget. Coordinator sockets keep the coupled default. |

Thresholds live as constants in the mains (tunable at the CLI only if a task proves
the need — YAGNI).

## 10. Testing

- **Codec units**: Heartbeat round-trip; `WorkResult` with `worker_id` round-trip;
  malformed rejection in the existing strict style.
- **Coordinator units** (fake transports + fake clock; no sleeps): death exactly at
  the threshold boundary; re-dispatch set = outstanding ∪ dead-completions; cap →
  permanent fail; duplicate-result drop; zero-live abort; no-heartbeat-at-start
  abort; STOP × live only; tombstone ignores zombie heartbeat.
- **Worker units** (fakes): one heartbeat per empty wait tick and one after each
  result; `worker_id` stamped into results; exit after 60 consecutive empty ticks;
  STOP still exits immediately.
- **Chaos E2E** (scripted against real data, Task 8 convention — day-files are
  gitignored so this is not a ctest): 2 workers × ≥3 day-files; `kill -9` one worker
  after its first result; assert the coordinator finishes with all files ok, exit 0,
  and merged survivor stores match the oracle counts. Recorded in
  `docs/validation-log.md`.
- **Regression**: entire existing suite stays green.

## 11. Success Criteria

1. Chaos run: mid-run `kill -9` of one of two workers → run completes, merged
   counts == oracle, coordinator exit 0.
2. Coordinator killed mid-run → all workers exit within their timeout budget; no
   orphans.
3. Full unit suite green, including new coordinator/worker/codec tests.
