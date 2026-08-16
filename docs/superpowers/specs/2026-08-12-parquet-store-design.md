# Parquet Event Store — Design Spec

Date: 2026-08-12
Status: approved
Parent spec: `2026-07-04-iiot-data-refinement-mas-design.md` (§6 schema, §10 idempotency, §14 Q4)
Branch: `feat/parquet-store`

## 1. Context & Problem

Three independent measurements now say the same thing, and none of them was
looking for it.

- The CUDA sweep on an RTX 4070: cleaning a month takes **1.82 s** on the GPU
  against 46.8 s single-threaded C++, but end to end `mono-1T` takes **230.45 s**.
  Persistence is **183.9 s — 79.8% of wall-clock**. Substituting the fastest
  known clean phase moves the total to 1.24x, and against the 8-thread C++
  already in the project it is **1.03x**.
- `merge_all` (PR #9) halved the merge phase and moved the scaling wall from
  1.11x to 1.84x — real, and still bounded by the same component.
- The GPU stage breakdown says it from inside: of CUDA's 1.82 s, 1.17 s is disk
  read and ~0.29 s is compute. The transform stopped being the cost before the
  pipeline did.

The store is where the time is, and the store is DuckDB with a UNIQUE index
maintained per row across 21.9M inserts, a write-ahead log, and a checkpoint.

This plan builds a second backend that has none of those, and measures whether
removing them is worth what it costs.

**It is a comparison, not a migration.** DuckDB stays the default. The
deliverable is a number that says which is better and under what conditions,
and it must be allowed to say Parquet loses.

## 2. Goals & Non-Goals

**Goals**

1. `ParquetEventStore`, an `IEventStore` that writes one Parquet file per
   day-file, with no index, no WAL and no constraint.
2. The analytics tier reads a Parquet store through the same eight tools,
   unmodified.
3. The existing test suite runs against both backends and must agree.
4. A benchmark row that compares the two on the persistence cost.

**Non-Goals**

- Migrating off DuckDB. The default is unchanged.
- Exporting a `.duckdb` from Parquet, compaction passes, date partitioning,
  or a GPU write path. None of them are needed to answer the question, and
  three of them are answers to a question this plan has not yet asked.
- Changing the event schema. Same ten columns, same semantics.

## 3. Key Decision — one Parquet file per day-file

DuckDB gave idempotency through `UNIQUE(machine_id, head_id, ts)` and
`INSERT OR IGNORE`: reprocessing a file cost index probes and produced no rows.
Parquet has no constraints, so the guarantee has to come from somewhere else.

It comes from the filename. Each day-file produces exactly one Parquet named
after it, so reprocessing **overwrites the same file**. Idempotency becomes a
property of the naming, checked by a test, rather than an invariant maintained
per row at runtime. That is the whole point: the cost being measured is the
per-row maintenance.

**What this does not cover, and how it is covered.** A worker declared dead
while still working has its file re-dispatched, so two workers can write the
same events into two differently-named files. Those rows agree in every column
(same input, same extraction), so the read view deduplicates:

```sql
CREATE VIEW cap_events AS
SELECT DISTINCT ON (machine_id, head_id, ts) *
FROM read_parquet('<dir>/*.parquet')
```

**The dedup moves from write time to read time, and that is a real cost, not a
free lunch.** On disjoint files the `DISTINCT ON` removes nothing and still pays
for a hash aggregation over 21.9M rows on every query. If that eats the write
saving, Parquet is the wrong answer and the benchmark has to say so. §7 measures
read as well as write for exactly this reason.

### 3.1 Consequence for the call sites

`IEventStore::write()` is called per batch and never learns that a file is
finished, so a store shared across 28 day-files would have to buffer 21.9M
events — about 1.4 GB — before it could name anything. Unacceptable.

So the store is constructed **inside the loop over input files**, one per
day-file, buffering ~50 MB at a time and flushing in its destructor. This
changes three call sites (`clean_main.cpp`, `monolith_main.cpp`,
`worker_main.cpp`), all of which already iterate over files.

## 4. Components

### 4.1 `ParquetEventStore` (`core/src/store/ParquetEventStore.cpp`)

```cpp
// One Parquet file per input day-file. No index, no WAL, no constraint --
// which is the point: this exists to measure what those cost.
class ParquetEventStore : public IEventStore {
public:
    // out_path is the .parquet to write; machine_id labels every row.
    ParquetEventStore(const std::string& out_path, const std::string& machine_id);
    ~ParquetEventStore() override;          // does NOT flush; see the note below

    void write(std::span<const CapEvent> events) override;   // buffers
    void close();                            // flush + report errors loudly
    long long count() const;                 // events accepted so far

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
```

Implementation: accumulate into an in-memory DuckDB table with no constraints,
then one `COPY (SELECT ...) TO '<path>' (FORMAT PARQUET)` on close. Reuses the
DuckDB dependency already present rather than adding Arrow, and the in-memory
table carries no index.

`close()` exists because a destructor cannot report failure.

> **[SUPERSEDED by the implementation.]** This spec had the destructor call
> `close()` and swallow what it throws. The shipped code does the opposite
> (`ParquetEventStore.cpp`): the destructor **abandons** an unclosed store and
> writes nothing, so an interrupted clean leaves no file rather than a partial
> day presented as a whole one. Publication happens only through an explicit,
> throwing `close()` — every caller does the close/abandon discipline itself.

Paths are spliced into SQL, so they go through the same `sql_quote` helper
`DuckDbEventStore` uses. A path with a quote in it must not truncate the
statement.

### 4.2 Store selection in the apps

A `--format duckdb|parquet` flag, defaulting to `duckdb`. Under `parquet`, the
destination argument is a **directory**, and each day-file writes
`<dir>/<input-basename>.parquet` into it.

### 4.3 `store.connect()` (`python/analytics/store.py`)

```python
def connect(cfg):
    """A read-only connection whose `cap_events` is the configured store.

    A directory means a Parquet store: the table becomes a view over its files,
    deduplicated on the event identity, and the eight tools cannot tell the
    difference -- none of their 15 `FROM cap_events` change.
    """
```

If `cfg.store_path` is a directory: open an in-memory connection and create the
view of §3. Otherwise open the `.duckdb` read-only, as today.

**No tool changes.** That is the property that makes the comparison honest —
the same SQL runs on both backends, so a difference in results is a defect in
one of them, not a difference in how they were queried.

## 5. Error Handling

- Destination directory missing under `--format parquet`: created, or a clear
  error if that fails. Never a partial write to the wrong place.
- `COPY` failure on close: `close()` throws with the DuckDB message; the
  destructor never publishes (see the superseded note above — it abandons
  rather than closing).
- Empty input (zero events): writes a valid empty Parquet with the right
  schema, so `read_parquet('*.parquet')` never fails on a glob that includes it.
- A Parquet store directory containing no files: `connect()` fails with a
  message naming the directory, rather than yielding an empty view that makes
  every tool report "insufficient data".

## 6. Testing

| # | Test | Asserts |
|---|---|---|
| T1 | `ParquetEventStore` round-trip | Events written, read back through `read_parquet`, all ten columns identical |
| T2 | Reprocessing a day-file | Same file overwritten, row count unchanged — §3's idempotency, by name |
| T3 | Re-dispatch duplication | Two files with identical events, view returns each event once |
| T4 | Empty input | Valid Parquet with the schema, glob still readable |
| T5 | Path with a single quote | Write and read both work |
| T6 | **Backend parity** | The analytics suite runs against a Parquet store and a DuckDB store built from the same events, and every tool returns the same values |

T6 is the load-bearing one. It reuses the existing fixtures rather than adding
expectations: the same tools, the same data, two formats. If they disagree, one
backend is wrong and the suite says which tool noticed.

## 7. Benchmark

The question is persistence, so both halves are measured.

**Write:** a `parquet` arch in the existing sweep, `e2e` mode, against the
183.9 s the DuckDB store costs on 21.9M rows.

**Read:** a new small harness timing the three canned reports (`kpi`, `drift`,
`anomalies`) against each backend. This is where the read-time `DISTINCT ON`
shows up, and it is the number that decides whether the write saving survives.

A result of "Parquet writes faster and reads slower, net worse" is a valid
outcome and must be reported as such.

## 8. Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | Read-time dedup eats the write saving | Measured in §7 rather than assumed. If it does, the finding is that DuckDB's write-time index is the cheaper place to pay, which is worth knowing. |
| R2 | `DISTINCT ON` over 21.9M rows exhausts memory on a large period | Measured at the three-month volume, which is the largest the project has. If it fails there, the view gains a `machine_id`/period pushdown before the dedup. |
| R3 | Buffering ~50 MB per day-file is worse for a much larger file | Bounded by one input file, and the pool's largest is 57.7 MB of CSV. A streaming writer is the fallback and is not built until needed. |
| R4 | Two backends double the surface where a defect can hide | T6 is exactly this test: the suite runs on both and they must agree. |

## 9. Success Criteria

1. `ParquetEventStore` passes T1-T5.
2. The analytics suite passes against a Parquet store with **no tool changes** (T6).
3. The sweep produces a `parquet` row at all three volumes, counts oracle-exact.
4. Read timings for all three report types on both backends.
5. `docs/bench/results.md` states which backend wins, on write, on read, and net —
   including the case where Parquet loses.
