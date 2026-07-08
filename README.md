# IIoT Data Refinement MAS — AROL Capping Machine Telemetry

Multi-agent system (MAS) for refining and analyzing Industrial IoT telemetry
from an AROL capping machine. Built for the **System and Device Programming**
course at Politecnico di Torino.

**Stack:** C++20 core · ZeroMQ IPC · DuckDB persistence · Python validation oracle

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
  - [Domain Types](#domain-types)
  - [Ingestion — CsvRawReader](#ingestion--csvrawreader)
  - [Dedup Engine — CapEventExtractor](#dedup-engine--capeventextractor)
  - [Persistence — IEventStore / CsvEventStore / DuckDbEventStore](#persistence--ieventstore--csveventstore--duckdbeventstore)
  - [Pipeline Orchestration — clean_file()](#pipeline-orchestration--clean_file)
  - [Agent Communication — Message Protocol](#agent-communication--message-protocol)
  - [Transport Layer — IMessageSource / IMessageSink / ZmqTransport](#transport-layer--imessagesource--imessagesink--zmqtransport)
  - [Cleaning Agent — CleaningWorker](#cleaning-agent--cleaningworker)
  - [Coordinator Agent — run_coordinator()](#coordinator-agent--run_coordinator)
- [Database Design](#database-design)
  - [DuckDB Schema](#duckdb-schema)
  - [Write Path (Staging + Merge)](#write-path-staging--merge)
  - [Idempotent Reprocessing](#idempotent-reprocessing)
  - [Cross-Worker Merge](#cross-worker-merge)
  - [Parquet Export](#parquet-export)
- [Executables](#executables)
  - [clean — Single-File Batch Pipeline](#clean--single-file-batch-pipeline)
  - [mas_coordinator — Ventilator + Sink Agent](#mas_coordinator--ventilator--sink-agent)
  - [mas_worker — Cleaning Agent](#mas_worker--cleaning-agent)
  - [mas_merge — Post-Run Store Unification](#mas_merge--post-run-store-unification)
- [Distributed Processing Flow](#distributed-processing-flow)
- [Python Validation Oracle](#python-validation-oracle)
- [Build & Run](#build--run)
  - [Prerequisites](#prerequisites)
  - [Build](#build)
  - [Run Tests](#run-tests)
  - [Single-File Processing](#single-file-processing)
  - [Distributed Multi-File Processing](#distributed-multi-file-processing)
  - [Validation Against Python Oracle](#validation-against-python-oracle)
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
- **Operator** — launches executables, inspects results
- **Python Oracle** — independent cross-check for correctness

### C4 Container (Level 2)

Shows the four C++ executables, the ZeroMQ fabric, database stores, and the
Python validation scripts.

![C4 Container Diagram](docs/diagrams/C4_Container.png)

**Containers:**
| Container | Type | Purpose |
|-----------|------|---------|
| `clean` | C++ CLI | Single-process batch pipeline (CSV or DuckDB output) |
| `mas_coordinator` | C++ CLI | Ventilator + sink: dispatches work, collects results |
| `mas_worker` | C++ CLI | Cleaning agent: processes assigned day-files |
| `mas_merge` | C++ CLI | Merges per-worker DuckDB stores into a unified database |
| ZeroMQ Fabric | libzmq 4.3.5 | PUSH/PULL sockets for work distribution and result collection |
| DuckDB Store | DuckDB | Persistent `cap_events` table with idempotent upserts |
| CSV Output | CSV file | Flat event file (alternative to DuckDB) |
| `oracle.py` | Python 3 | Reference dedup implementation |
| `validate_real.py` | Python 3 | Cross-validation script |

### C4 Component (Level 3)

Shows every class, interface, and function inside the C++ core libraries.

![C4 Component Diagram](docs/diagrams/C4_Component.png)

---

## Project Structure

```
.
├── CMakeLists.txt                   # Build system (CMake 3.16+, C++20)
├── README.md                        # This file
│
├── core/                            # C++ source code
│   ├── include/mas/                 # Public headers
│   │   ├── CapEvent.hpp             # Domain types: RawRow, CapEvent, NUM_HEADS
│   │   ├── CapEventExtractor.hpp    # Stateful per-head dedup engine
│   │   ├── CsvRawReader.hpp         # Raw telemetry CSV streaming reader
│   │   ├── EventStore.hpp           # IEventStore abstract interface (DIP seam)
│   │   ├── CsvEventStore.hpp        # CSV file persistence backend
│   │   ├── DuckDbEventStore.hpp     # DuckDB persistence backend (PIMPL)
│   │   ├── Pipeline.hpp             # clean_file() pipeline orchestrator
│   │   ├── Message.hpp              # Wire protocol: WorkItem, WorkResult, STOP
│   │   ├── Transport.hpp            # IMessageSource / IMessageSink interfaces
│   │   ├── CleaningWorker.hpp       # Agent loop: PULL work → clean → PUSH result
│   │   ├── Coordinator.hpp          # Ventilator + sink dispatcher
│   │   └── ZmqTransport.hpp         # ZeroMQ PUSH/PULL socket adapters
│   └── src/                         # Implementations
│       ├── CapEventExtractor.cpp
│       ├── CsvRawReader.cpp
│       ├── CsvEventStore.cpp
│       ├── DuckDbEventStore.cpp
│       ├── Pipeline.cpp
│       ├── Message.cpp
│       ├── CleaningWorker.cpp
│       ├── Coordinator.cpp
│       ├── ZmqTransport.cpp
│       ├── clean_main.cpp           # → clean executable
│       ├── coordinator_main.cpp     # → mas_coordinator executable
│       ├── worker_main.cpp          # → mas_worker executable
│       └── merge_main.cpp           # → mas_merge executable
│
├── tests/                           # Google Test unit tests
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
│       └── FakeTransport.hpp        # In-memory transport for unit tests
│
├── python/                          # Python validation oracle
│   ├── oracle.py                    # Reference dedup implementation
│   ├── test_oracle.py               # Pytest unit tests for the oracle
│   └── validate_real.py             # Cross-validation: C++ vs Python on real data
│
├── docs/
│   ├── validation-log.md            # Real-data test results log
│   └── diagrams/                    # C4 architecture diagrams
│       ├── c4-context.puml          # Level 1: System Context
│       ├── c4-container.puml        # Level 2: Container
│       ├── c4-component.puml        # Level 3: Component
│       ├── C4_Context.png           # Rendered context diagram
│       ├── C4_Container.png         # Rendered container diagram
│       └── C4_Component.png         # Rendered component diagram
│
├── telemetry_*/                     # Raw data (git-ignored, ~1.6 GB/month)
└── build/                           # CMake build directory (git-ignored)
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

### Domain Types

**File:** [`CapEvent.hpp`](core/include/mas/CapEvent.hpp)

- `NUM_HEADS = 36` — the AROL machine has 36 capping heads
- `RawRow` — one 1 Hz poll: timestamp + three arrays of 36 doubles (count, torque, status)
- `CapEvent` — one real cap event (or reset marker) with all fields
- `is_fault_status(double)` — checks for AROL Equatorque fault code 65

### Ingestion — CsvRawReader

**Files:** [`CsvRawReader.hpp`](core/include/mas/CsvRawReader.hpp) · [`CsvRawReader.cpp`](core/src/CsvRawReader.cpp)

Streams raw telemetry CSVs line by line. Expects **109 columns**: 1 timestamp +
36 Count + 36 AppTorque + 36 Status. Discards the header row on construction.
Silently skips truncated or malformed rows, tracking them via `skipped()`.

### Dedup Engine — CapEventExtractor

**Files:** [`CapEventExtractor.hpp`](core/include/mas/CapEventExtractor.hpp) · [`CapEventExtractor.cpp`](core/src/CapEventExtractor.cpp)

Stateful, per-head tracker. Maintains `last_count_[36]` (one `optional<long long>`
per head). On the first observation of each head, it seeds the counter without
emitting an event. Subsequent rows produce events only on counter transitions.

**Not thread-safe** — one instance per stream/partition.

### Persistence — IEventStore / CsvEventStore / DuckDbEventStore

**Files:**
- [`EventStore.hpp`](core/include/mas/EventStore.hpp) — abstract `write(span<CapEvent>)` interface (DIP seam)
- [`CsvEventStore.hpp`](core/include/mas/CsvEventStore.hpp) · [`CsvEventStore.cpp`](core/src/CsvEventStore.cpp) — CSV file backend
- [`DuckDbEventStore.hpp`](core/include/mas/DuckDbEventStore.hpp) · [`DuckDbEventStore.cpp`](core/src/DuckDbEventStore.cpp) — DuckDB backend (PIMPL)

The pipeline writes events through `IEventStore` and never sees which backend is
used. Implementations own the `machine_id`. The `DuckDbEventStore` uses the
PIMPL pattern to hide `duckdb.hpp` from callers.

### Pipeline Orchestration — clean_file()

**Files:** [`Pipeline.hpp`](core/include/mas/Pipeline.hpp) · [`Pipeline.cpp`](core/src/Pipeline.cpp)

Two overloads:
1. `clean_file(in_path, store)` — generic: reads CSV → extracts events → writes to any `IEventStore` in 8192-event batches. Returns event count or -1 on bad input.
2. `clean_file(in_path, out_path, machine_id)` — CSV convenience wrapper. Returns -1 (bad input) or -2 (bad output).

### Agent Communication — Message Protocol

**Files:** [`Message.hpp`](core/include/mas/Message.hpp) · [`Message.cpp`](core/src/Message.cpp)

Newline-separated wire format:

| Tag | Direction | Fields |
|-----|-----------|--------|
| `WORK` | Coordinator → Worker | `in_path` |
| `RESULT` | Worker → Coordinator | `in_path`, `events` (long long), `seconds` (double) |
| `STOP` | Coordinator → Worker | *(none — bare tag)* |

Full-consumption numeric validation (rejects trailing garbage like `"5x"`).

### Transport Layer — IMessageSource / IMessageSink / ZmqTransport

**Files:**
- [`Transport.hpp`](core/include/mas/Transport.hpp) — ISP-split interfaces: `IMessageSource::recv()` and `IMessageSink::send()`
- [`ZmqTransport.hpp`](core/include/mas/ZmqTransport.hpp) · [`ZmqTransport.cpp`](core/src/ZmqTransport.cpp) — ZeroMQ PUSH/PULL adapters

Only `ZmqTransport.hpp`, its `.cpp`, and the executable mains include `zmq.hpp`.
Domain code sees only the abstract interfaces (DIP boundary).

- `ZmqPushSink` — PUSH socket with configurable `send_timeout_ms` (and matching `ZMQ_LINGER`)
- `ZmqPullSource` — PULL socket with configurable `timeout_ms` (returns `nullopt` on expiry)

### Cleaning Agent — CleaningWorker

**Files:** [`CleaningWorker.hpp`](core/include/mas/CleaningWorker.hpp) · [`CleaningWorker.cpp`](core/src/CleaningWorker.cpp)

Agent loop:
1. `PULL` a `WorkItem` from the work queue
2. Invoke the injected `CleanFn(path, store)` — in production this is `mas::clean_file`
3. `PUSH` a `WorkResult` (path + event count + elapsed seconds)
4. Repeat until `STOP` or source exhaustion

`CleanFn` is injected via `std::function` so unit tests never touch the filesystem.

### Coordinator Agent — run_coordinator()

**Files:** [`Coordinator.hpp`](core/include/mas/Coordinator.hpp) · [`Coordinator.cpp`](core/src/Coordinator.cpp)

Single function combining ventilator + sink roles:
1. `PUSH` all `WorkItem` messages (one per day-file)
2. `PULL` one `WorkResult` per item (timeout → count stragglers as failed)
3. `PUSH` N `STOP` messages (one per worker) to shut down the pool
4. Return `DispatchSummary` with `total_events`, `files_ok`, `files_failed`

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
This was validated on real data: two runs of the same 86,399-row file both
produce 765,711 events, but the second run adds 0 new rows.

### Cross-Worker Merge

In distributed mode, each worker writes to its own `.duckdb` file (avoiding
concurrent single-writer conflicts). After all workers finish, `mas_merge`
unifies them:

```sql
ATTACH 'worker_N.duckdb' AS src (READ_ONLY);
INSERT OR IGNORE INTO cap_events SELECT * FROM src.cap_events;
DETACH src;
```

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
- `.duckdb` → uses `DuckDbEventStore` (probes input first to avoid creating an empty DB on missing files)
- anything else → uses `CsvEventStore`

Default `machine_id`: `"MCC"`.

### `mas_coordinator` — Ventilator + Sink Agent

```
usage: mas_coordinator <work_endpoint> <result_endpoint> <num_workers> <day1.csv> [day2.csv ...]
```

Binds PUSH socket on `work_endpoint`, binds PULL socket on `result_endpoint`.
60-second timeout on both send (no workers) and receive (straggler detection).

### `mas_worker` — Cleaning Agent

```
usage: mas_worker <work_endpoint> <result_endpoint> <out.duckdb> [machine_id]
```

Connects PULL to `work_endpoint`, connects PUSH to `result_endpoint`. Each worker
writes to its own DuckDB store. Blocks forever on recv (relies on coordinator's STOP).

### `mas_merge` — Post-Run Store Unification

```
usage: mas_merge <dst.duckdb> <machine_id> <src1.duckdb> [src2.duckdb ...]
```

Merges one or more per-worker stores into a unified destination. Idempotent:
running twice produces the same result.

---

## Distributed Processing Flow

```
┌─────────────────────┐
│   mas_coordinator    │
│ (ventilator + sink)  │
│                      │
│  PUSH work items     │──── tcp://127.0.0.1:5557 ────►┌───────────────┐
│  (one per day-file)  │                                │  mas_worker 1 │
│                      │                                │  → worker1.db │
│  PULL results        │◄── tcp://127.0.0.1:5558 ──────│               │
│  PUSH STOP × N       │──────────────────────────────►│               │
│                      │                                └───────────────┘
│                      │                                       ...
│                      │── tcp://127.0.0.1:5557 ────►┌───────────────┐
│                      │                              │  mas_worker N │
│                      │◄── tcp://127.0.0.1:5558 ────│  → workerN.db │
└─────────────────────┘                              └───────────────┘
         │
         │  (after all workers done)
         ▼
┌─────────────────────┐
│     mas_merge        │
│                      │
│  ATTACH worker1.db   │
│  ATTACH worker2.db   │──►  unified.duckdb
│  ...                 │
│  INSERT OR IGNORE    │
└─────────────────────┘
```

ZeroMQ's **PUSH/PULL** (pipeline pattern) provides automatic round-robin load
balancing across connected workers.

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

## Build & Run

### Prerequisites

- **CMake** ≥ 3.16
- **C++20** compiler (clang 14+, gcc 12+, MSVC 2022+)
- **Java** (for rendering PlantUML diagrams, optional)
- **Python 3** (for validation oracle, optional)

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

### Distributed Multi-File Processing

```bash
# Terminal 1 — start 2 workers
./build/mas_worker tcp://127.0.0.1:5557 tcp://127.0.0.1:5558 worker1.duckdb MCC &
./build/mas_worker tcp://127.0.0.1:5557 tcp://127.0.0.1:5558 worker2.duckdb MCC &

# Terminal 2 — start coordinator (must match num_workers)
./build/mas_coordinator tcp://127.0.0.1:5557 tcp://127.0.0.1:5558 2 \
    telemetry_*/2026-02-01.csv telemetry_*/2026-02-02.csv

# After completion — merge per-worker stores
./build/mas_merge unified.duckdb MCC worker1.duckdb worker2.duckdb
```

### Validation Against Python Oracle

```bash
# Count-only check
python3 python/oracle.py telemetry_*/2026-02-01.csv

# Cross-check C++ output
python3 python/validate_real.py telemetry_*/2026-02-01.csv events.csv
```

---

## Testing

The project has **10 Google Test files** covering all components:

| Test File | What It Tests |
|-----------|---------------|
| `test_cap_event_extractor.cpp` | Increment, aggregated, reset, held-dedup, first-observation seeding |
| `test_csv_raw_reader.cpp` | Happy path, truncated rows, malformed numerics, missing file |
| `test_pipeline.cpp` | End-to-end CSV→events flow, batch boundary, error codes |
| `test_duckdb_smoke.cpp` | DuckDB library linkage sanity |
| `test_duckdb_event_store.cpp` | Schema creation, write/count, idempotent upsert, merge_from, export_parquet |
| `test_zmq_smoke.cpp` | ZeroMQ library linkage sanity |
| `test_zmq_transport.cpp` | PUSH/PULL round-trip, timeout behavior |
| `test_message.cpp` | Encode/decode for WorkItem, WorkResult, STOP; malformed payload rejection |
| `test_cleaning_worker.cpp` | Agent loop with FakeTransport: work processing, STOP handling, failure reporting |
| `test_coordinator.cpp` | Dispatch + collect with FakeTransport: success, failure, timeout scenarios |

Test doubles: [`FakeTransport.hpp`](tests/fakes/FakeTransport.hpp) provides
in-memory `FakeSource`/`FakeSink` that replace ZeroMQ in unit tests.

---

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Dependency Inversion (DIP)** everywhere | `IEventStore`, `IMessageSource`, `IMessageSink` — domain code never depends on concrete implementations (DuckDB, ZeroMQ). Enables unit testing without I/O. |
| **PIMPL for DuckDbEventStore** | Hides `duckdb.hpp` (heavy header) from all compilation units except `DuckDbEventStore.cpp`. |
| **Per-worker DuckDB stores** | DuckDB is single-writer; distributed workers avoid file contention by writing isolated stores, merged post-run. |
| **INSERT OR IGNORE + UNIQUE key** | Idempotent reprocessing: re-running a day-file is always safe. |
| **Staging table for writes** | Bulk `Appender` into VARCHAR staging → `CAST(ts AS TIMESTAMP)` merge → strict timestamp validation (no partial corruption). |
| **8192-event batch writes** | Amortizes store overhead without excessive memory use. |
| **ZeroMQ PUSH/PULL** | Pipeline pattern with automatic round-robin load balancing across workers. |
| **Injected `CleanFn`** | `CleaningWorker` accepts any `std::function<long long(string, IEventStore&)>`, so tests inject a lambda that never touches the filesystem. |
| **Python oracle as separate implementation** | Independent re-implementation catches logic bugs that unit tests (which share the same assumptions) would miss. |

---

## Roadmap

- [ ] **Python analytics agents** — extend the MAS with analysis agents (trend detection, anomaly flagging)
- [ ] **Heartbeat-driven re-dispatch** — detect dead workers and re-assign their work items
- [ ] **Multi-process DuckDB concurrency** — explore DuckDB's concurrent access or use a shared store with WAL coordination
- [ ] **TRY_CAST + quarantine** — gracefully handle malformed timestamps (currently strict-CAST aborts the day-file)
- [ ] **Monitoring dashboard** — live view of processing progress and per-head statistics