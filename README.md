# IIoT Data Refinement MAS — AROL Capping Machine Telemetry

Multi-agent system (MAS) for refining and analyzing Industrial IoT telemetry
from an AROL capping machine. Built for the **System and Device Programming**
course at Politecnico di Torino.

**Stack:** C++20 core · ZeroMQ PUSH/PULL with heartbeat-driven liveness ·
DuckDB persistence · Python validation oracle · Chaos E2E resilience testing ·
Benchmark sweep harness

---

## Table of Contents

- [Problem Statement](#problem-statement)
- [Architecture Overview](#architecture-overview)
  - [C4 Context (Level 1)](#c4-context-level-1)
  - [C4 Container (Level 2)](#c4-container-level-2)
  - [C4 Component (Level 3)](#c4-component-level-3)
- [Project Structure](#project-structure)
- [Core Domain: The Dedup Transform](#core-domain-the-dedup-transform)
- [Components in Detail](#components-in-detail)
  - [Domain Layer](#domain-layer)
  - [Store Layer](#store-layer)
  - [Agent Layer](#agent-layer)
  - [Transport Layer](#transport-layer)
- [Database Design](#database-design)
  - [DuckDB Schema](#duckdb-schema)
  - [Write Path (Staging + Merge)](#write-path-staging--merge)
  - [Idempotent Reprocessing](#idempotent-reprocessing)
  - [Cross-Worker Merge](#cross-worker-merge)
  - [Parquet Export](#parquet-export)
- [Executables](#executables)
  - [clean — Single-File Batch Pipeline](#clean--single-file-batch-pipeline)
  - [mas_monolith — Multi-Threaded In-Process Pipeline](#mas_monolith--multi-threaded-in-process-pipeline)
  - [mas_coordinator — Ventilator + Sink + Liveness Monitor](#mas_coordinator--ventilator--sink--liveness-monitor)
  - [mas_worker — Cleaning Agent](#mas_worker--cleaning-agent)
  - [mas_merge — Post-Run Store Unification](#mas_merge--post-run-store-unification)
- [Resilience: Heartbeats, Death Detection, and Re-Dispatch](#resilience-heartbeats-death-detection-and-re-dispatch)
- [Distributed Processing Flow](#distributed-processing-flow)
- [Python Validation Oracle](#python-validation-oracle)
- [Benchmarking](#benchmarking)
- [Chaos E2E Testing](#chaos-e2e-testing)
- [Build & Run](#build--run)
  - [Prerequisites](#prerequisites)
  - [Build](#build)
  - [Run Tests](#run-tests)
  - [Single-File Processing](#single-file-processing)
  - [Monolith Multi-Threaded Processing](#monolith-multi-threaded-processing)
  - [Distributed Multi-File Processing](#distributed-multi-file-processing)
  - [Validation Against Python Oracle](#validation-against-python-oracle)
  - [Chaos E2E Test](#chaos-e2e-test)
  - [Performance Benchmark](#performance-benchmark)
- [Analytics CLI and Reports](#analytics-cli-and-reports)
  - [Build the analytics environment](#build-the-analytics-environment)
  - [Generate a report](#generate-a-report)
  - [Ask a question](#ask-a-question)
  - [Configuration (WP5)](#configuration-wp5)
  - [Reproduce the demo](#reproduce-the-demo)
- [Testing](#testing)
- [Design Decisions](#design-decisions)
- [Roadmap](#roadmap)

---

## Problem Statement

The AROL capping machine's PLC pipeline polls **36 capping heads** at **~1 Hz**
and uploads wide CSV day-files — each containing roughly **86,400 rows × 109
columns** per day (~1.6 GB/month zipped).

Most of this data is noise:

- **~24.5%** of rows are exact consecutive duplicates (the PLC re-reports the
  same state)
- The per-head **cap counters** only advance when a cap is actually applied; the
  vast majority of polls show a "held" (unchanged) counter

**The system's job:** Collapse this 1 Hz polling stream into the *real* cap
events — **one row per cap applied, per head, with correct timestamps** — then
persist, distribute, and analyze them.

---

## Architecture Overview

The system follows a **C4 model** architecture. PlantUML source files are in
[`docs/diagrams/`](docs/diagrams/) and rendered PNGs are shown below.

### C4 Context (Level 1)

Shows the MAS system boundary, external actors, and data flows.

![C4 Context Diagram](docs/diagrams/C4_Context.png)

**Key actors:**
- **AROL PLC** — uploads raw CSV day-files to the filesystem
- **Operator** — launches executables (single-file, monolith, or distributed MAS), inspects results
- **Python Oracle** — independent cross-check for correctness

### C4 Container (Level 2)

Shows the five C++ executables, the ZeroMQ fabric (now with 3 endpoints),
database stores, and the testing/validation scripts.

![C4 Container Diagram](docs/diagrams/C4_Container.png)

**Containers:**
| Container | Type | Purpose |
|-----------|------|---------|
| `clean` | C++ CLI | Single-process batch pipeline (CSV or DuckDB output) |
| `mas_monolith` | C++ CLI | Multi-threaded in-process pipeline (1T or MT modes) |
| `mas_coordinator` | C++ CLI | Ventilator + sink + liveness monitor with death detection |
| `mas_worker` | C++ CLI | Cleaning agent with heartbeat emission and idle-exit |
| `mas_merge` | C++ CLI | Merges per-worker/thread DuckDB stores (skips corrupt ones) |
| ZeroMQ Fabric | libzmq 4.3.5 | 3-endpoint PUSH/PULL: work, results, heartbeats |
| DuckDB Store | DuckDB | Persistent `cap_events` table with idempotent upserts |
| `chaos_e2e.sh` | Bash | Resilience test: SIGKILL a worker, verify full recovery |
| `run_bench.sh` | Bash | Performance sweep across architectures, threads, and volumes |

### C4 Component (Level 3)

Shows every class, interface, and function inside the C++ core libraries,
organized by layer.

![C4 Component Diagram](docs/diagrams/C4_Component.png)

---

## Project Structure

```
.
├── CMakeLists.txt                          # Build system (CMake 3.16+, C++20)
├── README.md                               # This file
│
├── core/                                   # C++ source code
│   ├── include/mas/
│   │   ├── domain/                         # Domain layer (pure logic, no I/O)
│   │   │   ├── CapEvent.hpp                # RawRow, CapEvent, NUM_HEADS, is_fault_status
│   │   │   ├── CapEventExtractor.hpp       # Stateful per-head dedup engine
│   │   │   └── Pipeline.hpp                # clean_file() orchestrator
│   │   ├── store/                          # Storage layer (persistence)
│   │   │   ├── EventStore.hpp              # IEventStore abstract interface (DIP seam)
│   │   │   ├── CsvRawReader.hpp            # Raw telemetry CSV streaming reader
│   │   │   ├── CsvEventStore.hpp           # CSV file persistence backend
│   │   │   └── DuckDbEventStore.hpp        # DuckDB persistence backend (PIMPL)
│   │   ├── agent/                          # Agent layer (MAS coordination)
│   │   │   ├── Message.hpp                 # Wire protocol: WorkItem, WorkResult, Heartbeat, STOP
│   │   │   ├── CleaningWorker.hpp          # Worker agent with heartbeats + idle-exit
│   │   │   └── Coordinator.hpp             # Coordinator with liveness + re-dispatch
│   │   └── transport/                      # Transport layer (ZeroMQ abstraction)
│   │       ├── Transport.hpp               # IMessageSource / IMessageSink interfaces
│   │       └── ZmqTransport.hpp            # ZMQ PUSH/PULL adapters (linger_ms control)
│   └── src/
│       ├── domain/                         # Domain implementations
│       │   ├── CapEventExtractor.cpp
│       │   └── Pipeline.cpp
│       ├── store/                          # Store implementations
│       │   ├── CsvRawReader.cpp
│       │   ├── CsvEventStore.cpp
│       │   └── DuckDbEventStore.cpp
│       ├── agent/                          # Agent implementations
│       │   ├── Message.cpp
│       │   ├── CleaningWorker.cpp
│       │   └── Coordinator.cpp
│       ├── transport/                      # Transport implementation
│       │   └── ZmqTransport.cpp
│       └── apps/                           # Executable entry points
│           ├── clean_main.cpp              # → clean
│           ├── monolith_main.cpp           # → mas_monolith
│           ├── coordinator_main.cpp        # → mas_coordinator
│           ├── worker_main.cpp             # → mas_worker
│           └── merge_main.cpp              # → mas_merge
│
├── tests/                                  # Google Test unit tests (63 tests)
│   ├── test_cap_event_extractor.cpp
│   ├── test_csv_raw_reader.cpp
│   ├── test_pipeline.cpp
│   ├── test_duckdb_smoke.cpp
│   ├── test_duckdb_event_store.cpp
│   ├── test_zmq_smoke.cpp
│   ├── test_zmq_transport.cpp
│   ├── test_message.cpp
│   ├── test_cleaning_worker.cpp
│   ├── test_coordinator.cpp
│   └── fakes/
│       └── FakeTransport.hpp               # FakeSource, FakeSink, FakeTickSource
│
├── python/                                 # Python validation oracle
│   ├── oracle.py                           # Reference dedup implementation
│   ├── test_oracle.py                      # Pytest unit tests for the oracle
│   └── validate_real.py                    # Cross-validation: C++ vs Python on real data
│
├── scripts/
│   └── chaos_e2e.sh                        # Resilience E2E: kill worker mid-run, verify recovery
│
├── bench/                                  # Performance benchmarking
│   ├── run_bench.sh                        # Sweep: mono {1,2,4,8}T + MAS {1..16}W × {1,7,28}d
│   ├── results.csv                         # Raw sweep data: 81 runs (27 configs × 3 repeats)
│   └── fixtures/
│       └── make_tiny_csvs.py               # Deterministic 2-row fixture generator
│
├── docs/
│   ├── validation-log.md                   # Real-data test results log
│   ├── bench/
│   │   └── results.md                      # Benchmark analysis: medians, scaling, bottleneck
│   └── diagrams/                           # C4 architecture diagrams
│       ├── c4-context.puml                 # Level 1: System Context
│       ├── c4-container.puml               # Level 2: Container
│       ├── c4-component.puml               # Level 3: Component
│       ├── C4_Context.png                  # Rendered context diagram
│       ├── C4_Container.png                # Rendered container diagram
│       └── C4_Component.png                # Rendered component diagram
│
├── telemetry_*/                            # Raw data (git-ignored, ~1.6 GB/month)
└── build/                                  # CMake build directory (git-ignored)
```

---

## Core Domain: The Dedup Transform

`CapEventExtractor` is the heart of the system. It scans each of the 36 heads'
`Count` column and emits one `CapEvent` per counter transition:

| Transition | Condition | Emitted Event |
|-----------|-----------|---------------|
| **Normal increment** | `c > last` and `delta == 1` | One event with `aggregated=false` |
| **Aggregated increment** | `c > last` and `delta > 1` | One event with `aggregated=true` (carousel advanced multiple counts between polls) |
| **Counter reset** | `c < last` | One event with `reset=true`, `delta=0` (PLC reset or rollover) |
| **Held** | `c == last` | *No event emitted* (no cap applied) |

Each event carries: `head_id` (1–36), `timestamp`, `cap_seq` (counter value),
`app_torque`, `status`, `delta`, `is_fault` (status == 65), `aggregated`, `reset`.

---

## Components in Detail

The C++ code is organized into **four layers** with strict dependency direction:

```
transport → agent → domain ← store
                      ↓
                  IEventStore (DIP seam)
```

### Domain Layer

**Directory:** `core/include/mas/domain/` · `core/src/domain/`

| Component | File(s) | Description |
|-----------|---------|-------------|
| `CapEvent` / `RawRow` | [`CapEvent.hpp`](core/include/mas/domain/CapEvent.hpp) | Domain value types. `NUM_HEADS=36`, `FAULT_STATUS=65`. |
| `CapEventExtractor` | [`CapEventExtractor.hpp`](core/include/mas/domain/CapEventExtractor.hpp) · [`.cpp`](core/src/domain/CapEventExtractor.cpp) | Stateful per-head dedup. Maintains `last_count_[36]`. Not thread-safe. |
| `clean_file()` | [`Pipeline.hpp`](core/include/mas/domain/Pipeline.hpp) · [`.cpp`](core/src/domain/Pipeline.cpp) | Orchestrator: CsvRawReader → CapEventExtractor → IEventStore in 8192-event batches. |

### Store Layer

**Directory:** `core/include/mas/store/` · `core/src/store/`

| Component | File(s) | Description |
|-----------|---------|-------------|
| `IEventStore` | [`EventStore.hpp`](core/include/mas/store/EventStore.hpp) | Abstract `write(span<CapEvent>)` interface (DIP seam). |
| `CsvRawReader` | [`CsvRawReader.hpp`](core/include/mas/store/CsvRawReader.hpp) · [`.cpp`](core/src/store/CsvRawReader.cpp) | Streams raw 109-column CSVs. Skips malformed rows with counter. |
| `CsvEventStore` | [`CsvEventStore.hpp`](core/include/mas/store/CsvEventStore.hpp) · [`.cpp`](core/src/store/CsvEventStore.cpp) | CSV file backend. Writes header on construction. |
| `DuckDbEventStore` | [`DuckDbEventStore.hpp`](core/include/mas/store/DuckDbEventStore.hpp) · [`.cpp`](core/src/store/DuckDbEventStore.cpp) | DuckDB backend (PIMPL). Staging → merge. `merge_from()` with try/catch DETACH. `export_parquet()`. |

### Agent Layer

**Directory:** `core/include/mas/agent/` · `core/src/agent/`

| Component | File(s) | Description |
|-----------|---------|-------------|
| `Message` / `WorkItem` / `WorkResult` / `Heartbeat` | [`Message.hpp`](core/include/mas/agent/Message.hpp) · [`.cpp`](core/src/agent/Message.cpp) | Wire protocol with tags `WORK`, `RESULT`, `HB`, `STOP`. `WorkResult` carries `worker_id` for attribution. `Heartbeat` carries `worker_id` + monotonic `seq`. |
| `CleaningWorker` | [`CleaningWorker.hpp`](core/include/mas/agent/CleaningWorker.hpp) · [`.cpp`](core/src/agent/CleaningWorker.cpp) | Agent loop with heartbeats: hello-beat on entry → PULL work → clean → PUSH result + beat. Idle-exit after `kIdleExitTicks=60` consecutive empty ticks (~60 s at 1 s recv timeout). |
| `run_coordinator()` | [`Coordinator.hpp`](core/include/mas/agent/Coordinator.hpp) · [`.cpp`](core/src/agent/Coordinator.cpp) | Ventilator + Sink + Liveness monitor. 4-phase loop: (1) result tick, (2) heartbeat drain, (3) death sweep + re-dispatch, (4) abort check. Injectable `ClockFn` for deterministic tests. `CoordinatorConfig`: `death_threshold=30s`, `redispatch_cap=2`. |

### Transport Layer

**Directory:** `core/include/mas/transport/` · `core/src/transport/`

| Component | File(s) | Description |
|-----------|---------|-------------|
| `IMessageSource` / `IMessageSink` | [`Transport.hpp`](core/include/mas/transport/Transport.hpp) | ISP-split interfaces. `recv()` returns `nullopt` on timeout. |
| `ZmqPushSink` | [`ZmqTransport.hpp`](core/include/mas/transport/ZmqTransport.hpp) · [`.cpp`](core/src/transport/ZmqTransport.cpp) | ZMQ PUSH adapter. Independent `linger_ms` parameter (default sentinel preserves backward compat; `linger_ms=0` for fire-and-forget sinks). |
| `ZmqPullSource` | (same files) | ZMQ PULL adapter. Configurable `timeout_ms`. |

---

## Database Design

### DuckDB Schema

```sql
CREATE TABLE IF NOT EXISTS cap_events (
    machine_id VARCHAR NOT NULL,
    head_id    SMALLINT NOT NULL,       -- 1..36
    ts         TIMESTAMP,               -- poll timestamp
    cap_seq    BIGINT NOT NULL,         -- head's Count value at this cap
    app_torque REAL,                     -- applied torque (Nm)
    status     REAL,                     -- AROL Equatorque status code
    delta      INTEGER,                  -- caps since last observation
    is_fault   BOOLEAN,                  -- true if status == 65
    aggregated BOOLEAN,                  -- true if delta > 1
    is_reset   BOOLEAN,                  -- true if counter reset/rollover
    UNIQUE (machine_id, head_id, cap_seq)
);
```

The `UNIQUE` constraint on `(machine_id, head_id, cap_seq)` is the backbone
of idempotent reprocessing.

### Write Path (Staging + Merge)

To avoid partial inserts on failure, `DuckDbEventStore` uses a **two-phase
write path**:

1. **Stage:** Batch-append events to `staging_cap_events` (VARCHAR timestamp,
   no UNIQUE constraint) using DuckDB's `Appender` API for bulk throughput.
2. **Merge:** `INSERT OR IGNORE INTO cap_events SELECT ... CAST(ts AS TIMESTAMP) FROM staging_cap_events`
   — the UNIQUE key silently drops duplicates.
3. **Cleanup:** `DELETE FROM staging_cap_events` clears the staging table.

On construction, any stale rows from a crashed run are cleared from staging.

### Idempotent Reprocessing

Re-running the same day-file against the same database produces **zero new rows**
thanks to `INSERT OR IGNORE` and the `UNIQUE(machine_id, head_id, cap_seq)` key.
Validated on real data: two runs of the same 86,399-row file both produce
765,711 events, but the second run adds 0 new rows.

### Cross-Worker Merge

In distributed mode (MAS or monolith-MT), each worker/thread writes to its own
`.duckdb` file. After completion, `mas_merge` (or the monolith's post-join
phase) unifies them:

```sql
ATTACH 'worker_N.duckdb' AS src (READ_ONLY);
INSERT OR IGNORE INTO cap_events SELECT * FROM src.cap_events;
DETACH src;
```

**Error handling:** `merge_from()` wraps the INSERT in try/catch and always
DETACHes `src` — a failed merge (corrupt store) doesn't poison subsequent
ATTACHes. `mas_merge` skips corrupt stores loudly instead of aborting.

**Precondition:** source stores must be closed/checkpointed before merge —
`ATTACH READ_ONLY` may not see another connection's unflushed WAL.

### Parquet Export

```sql
COPY (SELECT * FROM cap_events ORDER BY head_id, ts)
TO 'output.parquet' (FORMAT PARQUET);
```

Produces a columnar Parquet file for downstream analytics tools.

---

## Executables

### `clean` — Single-File Batch Pipeline

```
usage: clean <raw_in.csv> <events_out.csv|events_out.duckdb> [machine_id]
```

Processes a single raw CSV day-file. Detects output format by file extension:
- `.duckdb` → uses `DuckDbEventStore` (probes input first to avoid creating an empty DB)
- anything else → uses `CsvEventStore`

Default `machine_id`: `"MCC"`.

### `mas_monolith` — Multi-Threaded In-Process Pipeline

```
usage: mas_monolith <out.duckdb> <machine_id> <threads> <day1.csv> [day2.csv ...]
```

Two operating modes:

- **`threads=1` (mono-1T):** Sequential baseline — one DuckDB store, files processed one after another. No merge step.
- **`threads>1` (mono-MT):** Thread pool with atomic counter work-stealing. Each thread owns a per-thread `.duckdb` store (shared-nothing). After all threads join, the thread stores are merged into the output store. Uses `std::thread` + `std::atomic` (no ZeroMQ).

### `mas_coordinator` — Ventilator + Sink + Liveness Monitor

```
usage: mas_coordinator <work_endpoint> <result_endpoint> <hb_endpoint> <day1.csv> [day2.csv ...]
```

Binds three PUSH/PULL sockets:
- **Work** (PUSH, bind): sends `WorkItem` and `STOP` messages to workers
- **Results** (PULL, bind, 200 ms timeout): receives `WorkResult` messages — paces the loop
- **Heartbeats** (PULL, bind, 0 ms timeout): drained without blocking each tick

Death detection: workers silent > 30 s are tombstoned, their completed items re-opened, and all open items re-dispatched (up to 2 re-sends per item).

### `mas_worker` — Cleaning Agent

```
usage: mas_worker <work_endpoint> <result_endpoint> <hb_endpoint> <out.duckdb> <worker_id> [machine_id]
```

Connects to all three coordinator endpoints. Key behaviors:
- **1 s work recv timeout** — each empty tick emits a heartbeat
- **Idle-exit after 60 consecutive empty ticks** (~60 s) — exits cleanly if the coordinator vanishes
- **`linger_ms=0`** on result and heartbeat sinks — process exit is prompt (fixed a 121 s teardown bug found by chaos E2E)
- **Hello heartbeat** on `run()` entry — registers with the coordinator promptly

### `mas_merge` — Post-Run Store Unification

```
usage: mas_merge <dst.duckdb> <machine_id> <src1.duckdb> [src2.duckdb ...]
```

Merges one or more per-worker stores into a unified destination. **Crash-tolerant:** a corrupt source store (from a killed worker) is skipped with a warning instead of aborting. Idempotent: running twice produces the same result.

---

## Resilience: Heartbeats, Death Detection, and Re-Dispatch

The MAS implements a heartbeat-driven liveness protocol:

```
     ┌──────────────┐        Heartbeat (HB)        ┌──────────────┐
     │  mas_worker   │ ──────────────────────────►  │mas_coordinator│
     │               │        WorkResult            │               │
     │  PULL work    │ ──────────────────────────►  │  PULL results │
     │  PUSH result  │                              │  PULL HB      │
     │  PUSH HB      │◄──────────────────────────── │  PUSH work    │
     └──────────────┘        WorkItem / STOP        └──────────────┘
```

**Worker liveness contract:**
1. Hello heartbeat on `run()` entry
2. One heartbeat per empty recv tick (1 s period in production)
3. One heartbeat after each `WorkResult`
4. The only silent window is during `clean_file()` execution

**Coordinator 4-phase loop (per tick):**
1. **Result tick** — take one result (200 ms timeout paces the loop)
2. **Heartbeat drain** — drain all pending heartbeats without blocking
3. **Death sweep** — tombstone workers silent > `death_threshold` (30 s), reopen their completed items, re-dispatch all open items (capped at `redispatch_cap=2` per item)
4. **Abort check** — if no live workers remain and items are open, abort

**Dead-worker store write-off:** A dead worker's store is written off entirely
— its items are re-dispatched to survivors. The idempotent upsert absorbs any
overlap. `mas_merge` safely skips corrupt stores.

---

## Distributed Processing Flow

```
┌─────────────────────────┐
│     mas_coordinator      │
│ (ventilator + sink +     │
│  liveness monitor)       │
│                          │
│  PUSH WorkItems          │── tcp://..:5591 (work) ──►┌───────────────┐
│  PULL WorkResults        │◄─ tcp://..:5592 (results) │  mas_worker 1 │
│  PULL Heartbeats         │◄─ tcp://..:5593 (hb) ─────│  → w1.duckdb  │
│  Sweep deaths (30s)      │                            │  worker_id=w1 │
│  Re-dispatch dead items  │                            └───────────────┘
│  PUSH STOP × live workers│                                   ...
│                          │── tcp://..:5591 ──────►┌───────────────┐
│                          │◄─ tcp://..:5592 ───────│  mas_worker N │
│                          │◄─ tcp://..:5593 ───────│  → wN.duckdb  │
└─────────────────────────┘                         └───────────────┘
         │
         │  (after all workers done)
         ▼
┌─────────────────────────┐
│       mas_merge          │
│                          │
│  ATTACH w1.duckdb        │
│  ATTACH w2.duckdb        │──►  unified.duckdb
│  ...                     │
│  INSERT OR IGNORE        │
│  (skips corrupt stores)  │
└─────────────────────────┘
```

---

## Python Validation Oracle

An independent Python re-implementation of the dedup logic serves as a
**reference oracle** for cross-checking the C++ core:

| File | Purpose |
|------|---------|
| [`oracle.py`](python/oracle.py) | `extract(path)` → list of event tuples. Mirrors `CapEventExtractor` logic exactly. |
| [`test_oracle.py`](python/test_oracle.py) | Pytest: verifies increment counting, dedup of held rows, and aggregated detection. |
| [`validate_real.py`](python/validate_real.py) | Asserts C++ output row count matches oracle count on real data. |

**Validated result:** On 2026-02-01 day-file (86,399 rows): C++ = 765,711 events, Python = 765,711 events → **MATCH**.

---

## Benchmarking

The benchmark harness [`bench/run_bench.sh`](bench/run_bench.sh) runs a
comprehensive sweep:

- **Architectures:** monolith-1T, monolith-MT (threads ∈ {1,2,4,8}), MAS (workers ∈ {1,2,4,8,16})
- **Volumes:** 1, 7, or 28 day-files (`--quick` flag for 1-day only)
- **Repeats:** 3 per configuration
- **Per-run correctness check** against the oracle
- **Metrics captured:** clean time, merge time, total time, events, rows/s, events/s, peak RSS, CPU%

Raw data (81 runs = 27 configs × 3 repeats) lands in
[`bench/results.csv`](bench/results.csv); the analysis, with median tables and
caveats, is in [`docs/bench/results.md`](docs/bench/results.md). The fixture
generator [`bench/fixtures/make_tiny_csvs.py`](bench/fixtures/make_tiny_csvs.py)
creates deterministic 2-row test files with cross-day counter continuity for
smoke testing.

**Headline finding — the merge phase is the scaling wall.** Cleaning
parallelizes well (28-day medians: mono-1T 87.5 s → MAS N=8 27.0 s, a 3.2×
speedup), but unifying the per-worker stores costs 35–54 s at month scale and
*grows* with store count (MAS N=1 35 s → N≥4 ~50 s). End-to-end the MAS
therefore tops out at ~1.15× over the sequential baseline (N=8: 78.0 s vs
87.5 s), and mono-MT never meaningfully beats mono-1T. This is the measured
price of the "per-worker single-writer stores, merge at the sink" design: the
MAS earns its keep through crash isolation and scale-out across machines, not
through single-machine speedup.

All 81 runs matched the correctness oracle exactly.

---

## Chaos E2E Testing

[`scripts/chaos_e2e.sh`](scripts/chaos_e2e.sh) validates resilience under
real failure conditions:

1. Starts a coordinator + 2 workers on real day-files
2. **`kill -9`** worker 1 after 2 seconds (mid-processing)
3. Waits for the coordinator to detect the death (30 s threshold), re-dispatch items, and complete
4. Merges both worker stores (dead worker's store is harmlessly skipped or idempotently absorbed)
5. Asserts merged row count matches the oracle

**Result:** PASS — 2,290,233 events across 3 day-files, even with one worker
killed mid-run. Wall clock ~57 s (30 s death threshold dominates).

**Defect found:** Chaos testing exposed a 121 s teardown bug in orphan workers — connect-mode PUSH sockets with 60 s linger held undeliverable heartbeats. Fixed with `linger_ms=0`, regression-guarded by a unit test.

---

## Build & Run

### Prerequisites

- **CMake** ≥ 3.16
- **C++20** compiler (clang 14+, gcc 12+, MSVC 2022+)
- **Java** (for rendering PlantUML diagrams, optional)
- **Python 3** (for validation oracle and benchmark, optional)

All C++ dependencies are fetched automatically via CMake `FetchContent`:
- Google Test v1.14.0
- libzmq 4.3.5 + cppzmq 4.10.0
- DuckDB v1.2.2 (prebuilt binary — macOS universal or Linux amd64)

### Build

```bash
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j$(nproc)
```

### Run Tests

```bash
cd build && ctest --output-on-failure
```

### Single-File Processing

```bash
# Output as CSV
./build/clean telemetry_*/2026-02-01.csv events.csv MCC

# Output as DuckDB
./build/clean telemetry_*/2026-02-01.csv events.duckdb MCC
```

### Monolith Multi-Threaded Processing

```bash
# Single-threaded baseline (mono-1T)
./build/mas_monolith unified.duckdb MCC 1 telemetry_*/*.csv

# 4-thread parallel (mono-MT)
./build/mas_monolith unified.duckdb MCC 4 telemetry_*/*.csv
```

### Distributed Multi-File Processing

```bash
# Terminal 1 — start 2 workers (note: 3 endpoints now)
./build/mas_worker tcp://127.0.0.1:5591 tcp://127.0.0.1:5592 tcp://127.0.0.1:5593 \
    w1.duckdb w1 MCC &
./build/mas_worker tcp://127.0.0.1:5591 tcp://127.0.0.1:5592 tcp://127.0.0.1:5593 \
    w2.duckdb w2 MCC &

# Terminal 2 — start coordinator
./build/mas_coordinator tcp://127.0.0.1:5591 tcp://127.0.0.1:5592 tcp://127.0.0.1:5593 \
    telemetry_*/2026-02-01.csv telemetry_*/2026-02-02.csv

# After completion — merge per-worker stores
./build/mas_merge unified.duckdb MCC w1.duckdb w2.duckdb
```

### Validation Against Python Oracle

```bash
# Count-only check
python3 python/oracle.py telemetry_*/2026-02-01.csv

# Cross-check C++ output
python3 python/validate_real.py telemetry_*/2026-02-01.csv events.csv
```

### Chaos E2E Test

```bash
scripts/chaos_e2e.sh telemetry_*/2026-02-01.csv telemetry_*/2026-02-02.csv telemetry_*/2026-02-03.csv
```

### Performance Benchmark

```bash
# Full sweep (1, 7, 28-day volumes × all architectures × 3 repeats)
bench/run_bench.sh

# Quick sweep (1-day volume only)
bench/run_bench.sh --quick
```

---

## Analytics CLI and Reports

WP2–WP5. The C++ MAS above refines raw telemetry into a DuckDB store; this layer
answers questions about it and writes reports a human can hand over.

### Build the analytics environment

```bash
python3 -m venv .venv
.venv/bin/pip install -r python/requirements.txt
```

### Generate a report

```bash
scripts/arol report kpi       --period 2026-02          --config arol.json
scripts/arol report drift     --period 2026-02..2026-04 --config arol.json
scripts/arol report anomalies --period 2026-02          --config arol.json
```

Each writes a self-contained directory: `report.md` (source of truth),
`report.html` (portable, plots inlined as data URIs), `trace.json` (every tool
call with its arguments and row counts), and PNGs.

These three verbs run **fixed plans with no model in the loop** — the same store
and period gives the same report every time, apart from the generation timestamp
in the header. Committed examples are under
[`docs/reports/`](docs/reports/), and every number in them is reconciled against
a direct DuckDB query in the [validation log](docs/validation-log.md).

### Ask a question

```bash
export ANTHROPIC_API_KEY=...
scripts/arol ask "which head behaves differently, and why?" --period 2026-02
```

Claude chooses which tools to run and writes the narrative. **It never computes a
number**: every figure comes from the same deterministic SQL the `report` verbs
use, and the figures, the tool-call trace and the limits section are rendered
from the tool results regardless of what the model says.

With no API key, no network, a refusal, or a malformed plan, `ask` falls back to
a keyword router and the report's *Confidence and limits* section names the
reason. A model failure costs readability, never correctness.

### Configuration (WP5)

No path, band, or threshold is hard-coded. `arol.json`:

```json
{
  "store_path": "events_3mo.duckdb",
  "machine_id": "MCC",
  "torque_min": 1.5,
  "torque_max": 2.5,
  "mad_k": 3.0,
  "idle_min_seconds": 300,
  "model": "claude-opus-5",
  "effort": "high"
}
```

A configuration problem (unreadable config, unknown report type) exits 2 before
any work starts. An analysis gap — an empty period, a period the tools cannot
parse — is not an error: it produces a report whose limits section names the gap,
because a report generated unattended must still land on disk.

### Reproduce the demo

```bash
scripts/demo.sh
```

Loads the three-month store and generates all three report types into
`docs/reports/` — 20.3 M rows, three reports, ~8 s.

PDF export needs WeasyPrint (`pip install weasyprint`, plus Cairo/Pango); without
it, `--pdf` logs how to install it and writes Markdown and HTML as normal.

---

## Testing

The project has **73 C++ unit tests** across 11 Google Test files, plus **206
Python tests** for the analytics tier.

```bash
cd build && ctest --output-on-failure     # 73 C++ tests
cd python && ../.venv/bin/python -m pytest -q   # 206 Python tests
```

| Test File | What It Tests |
|-----------|---------------|
| `test_cap_event.cpp` | The status bitmask: a closure is rejected iff its status is odd (bit 0), across every condition in the brief's slide-6 table |
| `test_cap_event_extractor.cpp` | Increment, aggregated, reset, held-dedup, first-observation seeding |
| `test_csv_raw_reader.cpp` | Happy path, truncated rows, malformed numerics, missing file |
| `test_pipeline.cpp` | End-to-end CSV→events flow, batch boundary, error codes |
| `test_duckdb_smoke.cpp` | DuckDB library linkage sanity |
| `test_duckdb_event_store.cpp` | Schema creation, write/count, idempotent upsert, merge_from, export_parquet |
| `test_zmq_smoke.cpp` | ZeroMQ library linkage sanity |
| `test_zmq_transport.cpp` | PUSH/PULL round-trip, timeout behavior, zero-linger teardown regression |
| `test_message.cpp` | Encode/decode for WorkItem, WorkResult, Heartbeat, STOP; malformed payload rejection |
| `test_cleaning_worker.cpp` | Agent loop with FakeTransport: heartbeat emission, work processing, STOP handling, idle-exit countdown |
| `test_coordinator.cpp` | Liveness sweep: heartbeat refreshes, death detection, re-dispatch, dead-worker store write-off, redispatch cap, abort, deterministic ClockFn tests |

Test doubles: [`FakeTransport.hpp`](tests/fakes/FakeTransport.hpp) provides
`FakeSource`, `FakeSink`, and `FakeTickSource` (supports interleaved nullopt
ticks to model recv timeouts).

On the Python side, the analytics tools are tested against purpose-built DuckDB
fixtures — including degenerate ones (a head with no pass/fail verdicts, a head
with constant torque, a reject that carried no load) — and the agent's
orchestration is tested against an **injected fake API client**, so the suite
makes no network call and needs no API key. A golden-report test renders a fixed
plan over a fixed store and diffs the Markdown byte-for-byte, so a change in any
tool's SQL surfaces as a diff in a committed file rather than a silent shift in
a number.

---

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| **4-layer directory structure** | `domain/`, `store/`, `agent/`, `transport/` — strict dependency direction enforces separation of concerns. Domain code has zero I/O dependencies. |
| **Dependency Inversion (DIP)** | `IEventStore`, `IMessageSource`, `IMessageSink` — domain code never depends on concrete implementations. Enables unit testing without I/O. |
| **PIMPL for DuckDbEventStore** | Hides `duckdb.hpp` (heavy header) from all compilation units except `DuckDbEventStore.cpp`. |
| **Per-worker/thread DuckDB stores** | DuckDB is single-writer; distributed workers and monolith threads avoid file contention by writing isolated stores, merged post-run. |
| **INSERT OR IGNORE + UNIQUE key** | Idempotent reprocessing: re-running a day-file is always safe. Dead-worker re-dispatches produce harmless overlap. |
| **Heartbeat-driven liveness** | 3-endpoint design: dedicated HB channel drained without blocking. Workers beat on entry, per-tick, and per-result. Coordinator sweeps deaths with injectable `ClockFn` for deterministic testing. |
| **Dead-worker store write-off** | A dead worker's completed items are re-opened and re-dispatched. Its store is written off — `mas_merge` skips corrupt stores, and the idempotent upsert absorbs any partial overlap. |
| **`linger_ms=0` on worker sinks** | Connect-mode PUSH sockets create pipes immediately and queue sends below HWM even without a peer. Infinite linger delays teardown. `linger_ms=0` drops undeliverable heartbeats instantly at exit. |
| **Injectable `ClockFn`** | Coordinator tests drive deadlines by advancing a fake clock, no sleeping. |
| **`FakeTickSource`** | Models interleaved recv timeouts (nullopt entries) for testing idle-exit countdown without real timers. |
| **Monolith mode** | Thread-based alternative to the MAS for environments where multi-process ZeroMQ is overkill. Same file-grain work unit and shared-nothing store strategy. |
| **try/catch DETACH in merge_from** | A failed INSERT from one corrupt store must not leave `src` attached, poisoning subsequent ATTACHes in the same loop. |

---

## Roadmap

- [x] **Python analytics agents** — eight deterministic analysis tools, an LLM planner and narrator that cannot alter a number, and the `arol` CLI. See [Analytics CLI and Reports](#analytics-cli-and-reports).
- [ ] **Attack the merge bottleneck** — the benchmark's headline finding: partitioned Parquet output or a concurrent-writer store would remove the 35–54 s unification cost that currently caps end-to-end speedup at ~1.15×
- [ ] **PUB/SUB fan-out and REQ/REP registration** — the two ZeroMQ patterns from the spec that the current 3-endpoint PUSH/PULL fabric does not yet use
- [ ] **TRY_CAST + quarantine** — gracefully handle malformed timestamps (currently strict-CAST aborts the day-file)
- [ ] **Monitoring dashboard** — live view of processing progress and per-head statistics
- [ ] **Containerized deployment** — Docker Compose for coordinator + N workers