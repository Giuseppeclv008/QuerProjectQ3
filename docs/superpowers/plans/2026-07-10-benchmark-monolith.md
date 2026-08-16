# Benchmark & Monolith Baseline — Implementation Plan (Plan 5)

> **[SUPERSEDED 2026-08-11 — event identity.]** This document teaches
> `UNIQUE(machine_id, head_id, cap_seq)` as the store's identity. That key was
> discarded on 2026-08-11: the PLC's Count register resets mid-month, a closure
> recorded later can carry an already-used `cap_seq`, and keying on it dropped
> distinct physical caps onto older rows — February persisted 21,872,663 events
> as 14,372,237 rows, with 18,721 of head 1's colliding day-17 closures carrying
> a different torque. The store now keys on `UNIQUE(machine_id, head_id, ts)`
> and refuses a cap_seq-keyed store on open (`DuckDbEventStore.cpp`). See
> README § Database Design and docs/validation-log.md ("Event identity").
> The text below is preserved as written; read every `cap_seq` key claim
> through this notice.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A monolithic baseline binary sharing the MAS hot path, a sweep harness measuring mono(T) vs MAS(N) across data volumes with per-run correctness assertions, and the four spec-§9 plot families — Goal 5's scalability proof.

**Architecture:** `mas_monolith` reuses `clean_file` verbatim: T=1 streams every file into one store; T>1 runs a fixed pool of `std::thread`s pulling file indices off an atomic counter into thread-local stores merged by the existing idempotent `merge_from` (file-grain — the same unit of work as the MAS, so mono-MT(T) vs MAS(N) isolates the mechanism). A bash harness sweeps the matrix, parses `/usr/bin/time -l`, asserts every run's count against a cached oracle, and emits one CSV; a Python script renders plots + a median table.

**Tech Stack:** C++20, std::thread (`Threads::Threads`; **no OpenMP** — mechanism swap recorded in spec §4), DuckDB (vendored), bash, Python 3 + pandas + matplotlib.

**Spec:** `docs/superpowers/specs/2026-07-10-benchmark-monolith-design.md` — semantics authority. Read it first.

## Global Constraints

- **Module layout (spec §3b):** new CLI lands in `core/src/apps/monolith_main.cpp`; includes are nested (`mas/domain/Pipeline.hpp`, `mas/store/DuckDbEventStore.hpp`). No library-code changes expected; CMake gains `find_package(Threads REQUIRED)` and the new target only.
- **CLI (exact):** `mas_monolith <out.duckdb> <machine_id> <threads> <day1.csv> [day2.csv ...]` — usage exit 2, runtime error exit 1, success 0 (repo convention).
- **Stderr formats already relied on elsewhere (do not change):** `clean` emits `wrote <n> cap events; store now holds <m> rows`; `mas_merge` emits `merged <n> stores (<k> skipped); dst holds <rows> rows`; worker/coordinator CLIs per Plan 4.
- **`mas_monolith` stderr contract (harness parses it):** exactly one summary line
  `monolith: <files> files, <events> events, clean <clean_s> s, merge <merge_s> s, total <total_s> s, store holds <rows> rows`
  with seconds printed via `%.3f` formatting (use `snprintf` or iostream with `std::fixed << std::setprecision(3)`).
- **Matrix (spec §5, exact):** mono T∈{1,2,4,8}; MAS N∈{1,2,4,8,16}; volumes = first {1,7,28} day-files in date order; repeats R=3, median. `--quick` = 1-day column only.
- **Correctness gate:** every benchmark run asserts final store rows == cached oracle total for its volume; any mismatch fails the sweep. (Multi-file-into-one-store is already validated — Plan 3 Task 8 merged two day-files per worker store with oracle-exact totals; `cap_seq` is the machine counter value, monotonic across days, so no UNIQUE collisions.)
- **Ports:** bench MAS runs use `tcp://127.0.0.1:5591/5592/5593` (chaos uses 5571-73/5581-83 — keep disjoint).
- **Timing/RSS:** `/usr/bin/time -l` (macOS): parse `real` from its `%e`-style first line is NOT present — the line is `        X.XX real         Y.YY user         Z.ZZ sys`; peak RSS line is `  <bytes>  maximum resident set size`. Parse exactly these.
- **No GNU `timeout` on this machine.** Watchdogs are sleep + `kill -0` loops (chaos-script pattern).
- **Build & test:** `cmake --build build -j` then `./build/unit_tests` — suite green at every commit (currently 65 tests).
- **Data:** `telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02.zip` (repo root, 42 MB) → 28 day CSVs, gitignored. Disk guard: ≥2 GB free before extraction.
- **Commits:** conventional style, each ending with the project trailer:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- **Branch:** `feat/benchmark-monolith` off `main`.

## File Structure (locked)

| File | Change | Responsibility |
|---|---|---|
| `core/src/apps/monolith_main.cpp` | create | monolith CLI, both arch variants |
| `CMakeLists.txt` | modify | `Threads`, `monolith_exe` target |
| `bench/run_bench.sh` | create | sweep harness → `bench/results.csv` |
| `bench/fixtures/make_tiny_csvs.py` | create | deterministic 2-file smoke fixtures |
| `python/bench_plots.py` | create | plots + median table |
| `python/test_bench_plots.py` | create | unit test on synthetic results.csv |
| `bench/results.csv`, `docs/bench/*.png`, `docs/bench/results.md` | create (Task 5) | committed evidence |
| `docs/validation-log.md` | append (Task 6) | sweep record |

---

### Task 1: `mas_monolith` — sequential path (T=1) + fixtures + smoke

**Files:**
- Create: `core/src/apps/monolith_main.cpp`
- Create: `bench/fixtures/make_tiny_csvs.py`
- Modify: `CMakeLists.txt` (target block after `coordinator_exe`; `find_package(Threads REQUIRED)` near the top, after `project(...)`)

**Interfaces:**
- Consumes: `mas::clean_file(const std::string&, IEventStore&)` → `long long` events or -1 (`mas/domain/Pipeline.hpp`); `mas::DuckDbEventStore(path, machine_id)`, `.count()`, `.merge_from(path)` (`mas/store/DuckDbEventStore.hpp`).
- Produces (Tasks 2-3 rely on): the CLI + summary-line contract from Global Constraints; threads argument accepted but **this task only implements `threads == 1`** — any `threads > 1` exits 2 with `error: threads > 1 lands in the next commit` (placeholder rejection keeps the contract honest until Task 2).

- [ ] **Step 1: Write the fixture generator**

`bench/fixtures/make_tiny_csvs.py`:

```python
#!/usr/bin/env python3
"""Two deterministic 109-column day-file fixtures for monolith smokes.

Shape matches the real telemetry files (ts + 36 counts + 36 torques + 36
statuses). Day 1 takes head 1's counter 1 -> 2, day 2 CONTINUES it 2 -> 3 —
mirroring real day-files, whose machine counter is monotonic across days.
(cap_seq IS that counter value; if day 2 replayed 1 -> 2 the idempotent
UNIQUE(machine, head, cap_seq) store would dedupe the second event, and a
2-file run would report 2 events but hold only 1 row — caught at execution
time on 2026-07-10.) Expected totals: 1 event per file, 2 events and 2 rows
for both files together.
"""
import sys, pathlib

def write_fixture(path: pathlib.Path, ts0: str, ts1: str,
                  c0: int, c1: int) -> None:
    header = ",".join(["ts"] + [f"c{i}" for i in range(1, 109)])
    def row(ts: str, head1_count: int) -> str:
        counts = [str(head1_count)] + ["0"] * 35
        torques = ["2.0"] * 36
        stats = ["2.0"] * 36
        return ",".join([ts] + counts + torques + stats)
    path.write_text(header + "\n" + row(ts0, c0) + "\n" + row(ts1, c1) + "\n")

def main() -> None:
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    write_fixture(out / "tiny-day1.csv", "2026-02-01T00:00:00",
                  "2026-02-01T00:00:01", 1, 2)
    write_fixture(out / "tiny-day2.csv", "2026-02-02T00:00:00",
                  "2026-02-02T00:00:01", 2, 3)
    print(f"wrote {out}/tiny-day1.csv {out}/tiny-day2.csv")

if __name__ == "__main__":
    main()
```

Column-order note for the implementer: the reader (`CsvRawReader.cpp`) splits on ',' — confirmed at execution time; header is positional, labels meaningless — Plan 4 Task 6's smoke used exactly this 109-column shape and the worker cleaned 1 event from it. If the pipeline fixture disagrees with the layout above, match the pipeline fixture and adjust the expected totals accordingly; state what you found in your report.

- [ ] **Step 2: Write the main (sequential only)**

`core/src/apps/monolith_main.cpp`:

```cpp
#include "mas/domain/Pipeline.hpp"
#include "mas/store/DuckDbEventStore.hpp"
#include <chrono>
#include <exception>
#include <iomanip>
#include <iostream>
#include <string>
#include <vector>

namespace {

double seconds_since(std::chrono::steady_clock::time_point t0) {
    return std::chrono::duration<double>(std::chrono::steady_clock::now() - t0)
        .count();
}

} // namespace

int main(int argc, char** argv) {
    if (argc < 5) {
        std::cerr << "usage: mas_monolith <out.duckdb> <machine_id> <threads> "
                     "<day1.csv> [day2.csv ...]\n";
        return 2;
    }
    const std::string out = argv[1], machine = argv[2];
    int threads = 0;
    try {
        threads = std::stoi(argv[3]);
    } catch (const std::exception&) {
        std::cerr << "error: threads must be a number\n";
        return 2;
    }
    if (threads < 1) {
        std::cerr << "error: threads must be >= 1\n";
        return 2;
    }
    if (threads > 1) {
        std::cerr << "error: threads > 1 lands in the next commit\n";
        return 2;
    }
    std::vector<std::string> files;
    for (int i = 4; i < argc; ++i) files.emplace_back(argv[i]);

    try {
        // Baseline arch "mono-1T" (spec §4): one process, one store, one file
        // after another — the exact hot path the MAS workers run, minus IPC.
        const auto t0 = std::chrono::steady_clock::now();
        mas::DuckDbEventStore store(out, machine);
        long long events = 0;
        for (const auto& f : files) {
            const long long n = mas::clean_file(f, store);
            if (n < 0) {
                std::cerr << "error: cannot clean " << f << "\n";
                return 1;
            }
            events += n;
        }
        const double clean_s = seconds_since(t0);
        const double merge_s = 0.0;   // sequential path writes one store: no merge
        std::cerr << "monolith: " << files.size() << " files, " << events
                  << " events, clean " << std::fixed << std::setprecision(3)
                  << clean_s << " s, merge " << merge_s << " s, total "
                  << (clean_s + merge_s) << " s, store holds " << store.count()
                  << " rows\n";
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << "\n";
        return 1;
    }
}
```

- [ ] **Step 3: CMake wiring**

In `CMakeLists.txt`, after `project(...)`/standard block add:

```cmake
find_package(Threads REQUIRED)
```

and after the `coordinator_exe` block add:

```cmake
add_executable(monolith_exe core/src/apps/monolith_main.cpp)
target_link_libraries(monolith_exe PRIVATE mas_core Threads::Threads)
set_target_properties(monolith_exe PROPERTIES OUTPUT_NAME mas_monolith)
```

and extend the existing `set_target_properties(... BUILD_RPATH ...)` list to include `monolith_exe`.

- [ ] **Step 4: Build + smoke (RED first: threads=2 rejected; GREEN: T=1 counts)**

```bash
cmake --build build -j 2>&1 | tail -3
F=/tmp/mas_bench_fix && rm -rf $F && python3 bench/fixtures/make_tiny_csvs.py $F
./build/mas_monolith /tmp/mono1.duckdb MCC 2 $F/tiny-day1.csv; echo "exit=$?"   # expect exit=2, "next commit" note
rm -f /tmp/mono1.duckdb*
./build/mas_monolith /tmp/mono1.duckdb MCC 1 $F/tiny-day1.csv $F/tiny-day2.csv; echo "exit=$?"
```

Expected final run: exit=0 and the exact summary line with `2 files, 2 events, ... store holds 2 rows`.
Also run the usage/exit-code contract: no args → exit 2; unreadable input file → exit 1 with `error: cannot clean ...`.

- [ ] **Step 5: Full suite regression**

Run: `./build/unit_tests` — all 65 PASS (nothing links the new main).

- [ ] **Step 6: Commit**

```bash
git add core/src/apps/monolith_main.cpp bench/fixtures/make_tiny_csvs.py CMakeLists.txt
git commit -m "feat(apps): mas_monolith sequential baseline (mono-1T) + smoke fixtures"
```

---

### Task 2: `mas_monolith` — threaded path (mono-MT)

**Files:**
- Modify: `core/src/apps/monolith_main.cpp`

**Interfaces:**
- Consumes: Task 1's main; `DuckDbEventStore::merge_from` (idempotent, source must be closed — thread stores are destroyed before merging, satisfying the documented precondition).
- Produces: full CLI contract — `threads > 1` runs the pool; summary line unchanged in shape; thread-local stores named `<out>.t<k>.duckdb` are left on disk after the merge (harness wipes its run dir).

- [ ] **Step 1: Replace the `threads > 1` rejection with the pool**

Replace the whole `try { ... }` body so both variants share the summary print. Full new body:

```cpp
    try {
        const auto t0 = std::chrono::steady_clock::now();
        long long events = 0;
        double clean_s = 0.0, merge_s = 0.0;
        long long rows = 0;

        if (threads == 1) {
            // Baseline arch "mono-1T": one store, one file after another.
            mas::DuckDbEventStore store(out, machine);
            for (const auto& f : files) {
                const long long n = mas::clean_file(f, store);
                if (n < 0) {
                    std::cerr << "error: cannot clean " << f << "\n";
                    return 1;
                }
                events += n;
            }
            clean_s = seconds_since(t0);
            rows = store.count();
        } else {
            // Arch "mono-MT" (spec §3): fixed pool of T threads pulling file
            // indices off an atomic counter — the same file-grain unit of
            // work as the MAS, threads instead of processes. Shared-nothing:
            // each clean_file call builds its own reader/extractor, and each
            // thread owns one store file, merged after the join (DuckDB is
            // single-writer; same strategy as the MAS sink).
            std::vector<long long> per_file(files.size(), 0);
            std::atomic<std::size_t> next{0};
            std::atomic<bool> failed{false};
            auto pull = [&](int t) {
                try {
                    mas::DuckDbEventStore local(
                        out + ".t" + std::to_string(t) + ".duckdb", machine);
                    for (std::size_t i;
                         (i = next.fetch_add(1)) < files.size();) {
                        per_file[i] = mas::clean_file(files[i], local);
                        if (per_file[i] < 0) failed = true;
                    }
                } catch (const std::exception& e) {
                    std::cerr << "error: thread " << t << ": " << e.what()
                              << "\n";
                    failed = true;
                }
            };
            {
                std::vector<std::thread> pool;
                pool.reserve(static_cast<std::size_t>(threads));
                for (int t = 0; t < threads; ++t) pool.emplace_back(pull, t);
                for (auto& th : pool) th.join();
            }
            clean_s = seconds_since(t0);
            if (failed) {
                for (std::size_t i = 0; i < files.size(); ++i)
                    if (per_file[i] < 0)
                        std::cerr << "error: cannot clean " << files[i] << "\n";
                return 1;
            }
            for (const auto n : per_file) events += n;

            // Thread stores are closed (destroyed) here — merge_from's
            // closed/checkpointed precondition holds.
            const auto tm = std::chrono::steady_clock::now();
            mas::DuckDbEventStore store(out, machine);
            for (int t = 0; t < threads; ++t)
                store.merge_from(out + ".t" + std::to_string(t) + ".duckdb");
            merge_s = seconds_since(tm);
            rows = store.count();
        }

        std::cerr << "monolith: " << files.size() << " files, " << events
                  << " events, clean " << std::fixed << std::setprecision(3)
                  << clean_s << " s, merge " << merge_s << " s, total "
                  << (clean_s + merge_s) << " s, store holds " << rows
                  << " rows\n";
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << "\n";
        return 1;
    }
```

Add includes: `<atomic>`, `<thread>`, `<cstddef>`. Delete the Task 1 `threads > 1` early-exit block. Path note: `DuckDbEventStore`'s ctor uses the path verbatim (no extension magic — see `worker_main.cpp`), so the thread-store string `out + ".t" + std::to_string(t) + ".duckdb"` must be byte-identical at construction and at `merge_from` — it is, in the code above. (An empty thread store — pool larger than file count — merges harmlessly: 0 rows.)

- [ ] **Step 2: RED→GREEN smoke**

```bash
cmake --build build -j 2>&1 | tail -3
F=/tmp/mas_bench_fix && python3 bench/fixtures/make_tiny_csvs.py $F
rm -f /tmp/monoT.duckdb*
./build/mas_monolith /tmp/monoT.duckdb MCC 2 $F/tiny-day1.csv $F/tiny-day2.csv; echo "exit=$?"
rm -f /tmp/mono1.duckdb*
./build/mas_monolith /tmp/mono1.duckdb MCC 1 $F/tiny-day1.csv $F/tiny-day2.csv; echo "exit=$?"
```

Expected: both exit=0; both report `2 files, 2 events, ... store holds 2 rows`; T=2 run leaves `/tmp/monoT.duckdb.t0.duckdb` and `.t1.duckdb` behind. Also: T=4 with 2 files (oversized pool) → same counts; unreadable file with T=2 → exit 1.

- [ ] **Step 3: Full suite regression**

Run: `./build/unit_tests` — 65 PASS.

- [ ] **Step 4: Commit**

```bash
git add core/src/apps/monolith_main.cpp
git commit -m "feat(apps): mas_monolith threaded path (mono-MT) — std::thread pool, thread-local stores, idempotent merge"
```

---

### Task 3: `bench/run_bench.sh` — sweep harness

**Files:**
- Create: `bench/run_bench.sh` (chmod +x)

**Interfaces:**
- Consumes: `mas_monolith` summary line (Task 2), `clean`/`mas_merge`/`mas_worker`/`mas_coordinator` CLIs and stderr formats (Global Constraints), day-file zip.
- Produces: `bench/results.csv` with header
  `arch,n_workers,threads,files,repeat,clean_s,merge_s,total_s,events,rows_per_s,events_per_s,peak_rss_mb,cpu_pct`
  (Task 4's plot script consumes exactly these columns).

- [ ] **Step 1: Write the harness**

```bash
#!/usr/bin/env bash
# Benchmark sweep (spec §5): mono T∈{1,2,4,8} + MAS N∈{1,2,4,8,16} across
# volumes {1,7,28} day-files, R=3 repeats, per-run correctness vs oracle.
# usage: bench/run_bench.sh [--quick]     (--quick = 1-day volume only)
set -euo pipefail

QUICK=0
[ "${1:-}" = "--quick" ] && QUICK=1
BUILD="${BUILD_DIR:-build}"
ZIP="telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02.zip"
DATA="${DATA_DIR:-telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02}"
MACHINE="MCC777eda3db57348ef8a3113a642ae74db"
OUT_CSV="bench/results.csv"
ROWS_PER_DAY=86399
REPEATS=3
WORK=tcp://127.0.0.1:5591 RES=tcp://127.0.0.1:5592 HB=tcp://127.0.0.1:5593

for exe in mas_monolith mas_merge mas_worker mas_coordinator clean; do
    [ -x "$BUILD/$exe" ] || { echo "missing $BUILD/$exe (build first)"; exit 2; }
done

# --- disk guard: >= 2 GB free before any extraction -------------------------
free_kb=$(df -k . | awk 'NR==2 {print $4}')
[ "$free_kb" -ge $((2 * 1024 * 1024)) ] || {
    echo "ABORT: only $((free_kb / 1024)) MB free; need >= 2048 MB"; exit 2; }

# --- volume prep: idempotent extraction -------------------------------------
mkdir -p "$DATA"
unzip -n -q "$ZIP" -d "$DATA" 2>/dev/null || unzip -n -q "$ZIP"   # zip layout may or may not nest
FILES=()
while IFS= read -r f; do FILES+=("$f"); done \
    < <(find "$DATA" -name '*.csv' | sort)
[ "${#FILES[@]}" -ge 28 ] || { echo "ABORT: ${#FILES[@]} CSVs found, need 28"; exit 2; }

VOLUMES=(1 7 28)
[ "$QUICK" = 1 ] && VOLUMES=(1)

T="$(mktemp -d /tmp/mas_bench.XXXXXX)"
PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do kill -9 "$p" 2>/dev/null || true; done; rm -rf "$T"; }
trap cleanup EXIT

# --- oracle cache: sequential `clean` totals per volume ----------------------
declare -A ORACLE
for v in "${VOLUMES[@]}"; do
    total=0
    for ((i = 0; i < v; i++)); do
        rm -f "$T/o.duckdb" "$T/o.duckdb.wal"
        n="$("$BUILD/clean" "${FILES[$i]}" "$T/o.duckdb" 2>&1 >/dev/null \
             | sed -n 's/^wrote \([0-9][0-9]*\) cap events.*/\1/p')"
        case "$n" in (*[!0-9]*|"") echo "oracle failed for ${FILES[$i]}: '$n'"; exit 1;; esac
        total=$((total + n))
    done
    ORACLE[$v]=$total
    echo "oracle[$v days] = $total events"
done

mkdir -p bench
echo "arch,n_workers,threads,files,repeat,clean_s,merge_s,total_s,events,rows_per_s,events_per_s,peak_rss_mb,cpu_pct" > "$OUT_CSV"

# parse /usr/bin/time -l output file -> "real user sys rss_bytes"
parse_time() {   # $1 = time-output file
    awk '
        /real/ && /user/ && /sys/ { real=$1; user=$3; sys=$5 }
        /maximum resident set size/ { rss=$1 }
        END { printf "%s %s %s %s", real, user, sys, rss }
    ' "$1"
}

emit_row() {   # arch n threads files repeat clean_s merge_s total_s events rss_bytes user sys real
    local arch=$1 n=$2 th=$3 nf=$4 rep=$5 clean=$6 merge=$7 total=$8 ev=$9 rss=${10} user=${11} sys=${12} real=${13}
    local rows=$((ROWS_PER_DAY * nf))
    python3 - "$arch" "$n" "$th" "$nf" "$rep" "$clean" "$merge" "$total" "$ev" "$rss" "$user" "$sys" "$real" "$rows" "$OUT_CSV" <<'PY'
import sys
(arch, n, th, nf, rep, clean, merge, total, ev, rss, user, sys_t, real, rows, out) = sys.argv[1:16]
total_f = float(total)
row = ",".join([
    arch, n, th, nf, rep,
    f"{float(clean):.3f}", f"{float(merge):.3f}", f"{total_f:.3f}", ev,
    f"{float(rows)/total_f:.1f}", f"{float(ev)/total_f:.1f}",
    f"{float(rss)/1048576:.1f}",
    f"{100.0*(float(user)+float(sys_t))/float(real):.1f}",
])
open(out, "a").write(row + "\n")
PY
}

check_count() {   # $1 = actual rows, $2 = volume, $3 = label
    [ "$1" = "${ORACLE[$2]}" ] || {
        echo "FAIL: $3: rows $1 != oracle ${ORACLE[$2]}"; exit 1; }
}

# --- monolith runs -----------------------------------------------------------
for v in "${VOLUMES[@]}"; do
    for th in 1 2 4 8; do
        for rep in 1 2 3; do
            R="$T/run" && rm -rf "$R" && mkdir "$R"
            /usr/bin/time -l "$BUILD/mas_monolith" "$R/mono.duckdb" "$MACHINE" "$th" \
                "${FILES[@]:0:$v}" 2>"$R/log" || { cat "$R/log"; exit 1; }
            line=$(grep '^monolith:' "$R/log")
            ev=$(echo "$line"    | sed -n 's/.* files, \([0-9]*\) events.*/\1/p')
            clean=$(echo "$line" | sed -n 's/.*clean \([0-9.]*\) s.*/\1/p')
            merge=$(echo "$line" | sed -n 's/.*merge \([0-9.]*\) s.*/\1/p')
            total=$(echo "$line" | sed -n 's/.*total \([0-9.]*\) s.*/\1/p')
            rows=$(echo "$line"  | sed -n 's/.*store holds \([0-9]*\) rows.*/\1/p')
            check_count "$rows" "$v" "mono T=$th v=$v rep=$rep"
            read -r real user sys rss < <(parse_time "$R/log")
            arch=$([ "$th" = 1 ] && echo mono-1T || echo mono-MT)
            emit_row "$arch" 0 "$th" "$v" "$rep" "$clean" "$merge" "$total" "$ev" "$rss" "$user" "$sys" "$real"
            echo "done: $arch T=$th v=${v}d rep=$rep total=${total}s"
        done
    done
done

# --- MAS runs ----------------------------------------------------------------
for v in "${VOLUMES[@]}"; do
    for n in 1 2 4 8 16; do
        for rep in 1 2 3; do
            R="$T/run" && rm -rf "$R" && mkdir "$R"
            t_start=$(python3 -c 'import time; print(f"{time.time():.3f}")')
            WPIDS=()
            for ((w = 1; w <= n; w++)); do
                /usr/bin/time -l "$BUILD/mas_worker" "$WORK" "$RES" "$HB" \
                    "$R/w$w.duckdb" "w$w" 2>"$R/w$w.log" &
                WPIDS+=($!); PIDS+=($!)
            done
            /usr/bin/time -l "$BUILD/mas_coordinator" "$WORK" "$RES" "$HB" \
                "${FILES[@]:0:$v}" 2>"$R/coord.log" || { cat "$R/coord.log"; exit 1; }
            for p in "${WPIDS[@]}"; do wait "$p" || true; done
            t_clean=$(python3 -c "import time; print(f'{time.time() - $t_start:.3f}')")

            srcs=(); for ((w = 1; w <= n; w++)); do srcs+=("$R/w$w.duckdb"); done
            t_m0=$(python3 -c 'import time; print(f"{time.time():.3f}")')
            /usr/bin/time -l "$BUILD/mas_merge" "$R/merged.duckdb" "$MACHINE" \
                "${srcs[@]}" 2>"$R/merge.log" || { cat "$R/merge.log"; exit 1; }
            t_merge=$(python3 -c "import time; print(f'{time.time() - $t_m0:.3f}')")

            rows=$(sed -n 's/.*dst holds \([0-9]*\) rows.*/\1/p' "$R/merge.log")
            check_count "$rows" "$v" "MAS N=$n v=$v rep=$rep"
            ev=$(sed -n 's/.* failed, \([0-9]*\) events.*/\1/p' "$R/coord.log")
            grep -q ' 0 failed' "$R/coord.log" || { echo "FAIL: files failed"; cat "$R/coord.log"; exit 1; }

            # RSS = sum of per-process maxima; CPU% = aggregate (user+sys)/coordinator-real
            rss=0; user_sum=0; sys_sum=0
            for lg in "$R"/w*.log "$R/coord.log" "$R/merge.log"; do
                read -r lreal luser lsys lrss < <(parse_time "$lg")
                rss=$((rss + lrss))
                user_sum=$(python3 -c "print($user_sum + $luser)")
                sys_sum=$(python3 -c "print($sys_sum + $lsys)")
            done
            read -r creal _ _ _ < <(parse_time "$R/coord.log")
            total=$(python3 -c "print(f'{$t_clean + $t_merge:.3f}')")
            emit_row "mas" "$n" 1 "$v" "$rep" "$t_clean" "$t_merge" "$total" "$ev" "$rss" "$user_sum" "$sys_sum" "$creal"
            echo "done: mas N=$n v=${v}d rep=$rep total=${total}s"
        done
    done
done

echo "sweep complete: $(( $(wc -l < "$OUT_CSV") - 1 )) rows in $OUT_CSV"
```

Two verification duties while implementing (do them by running, not guessing; note results in your report):
1. `/usr/bin/time -l` writes to the SAME stderr stream as the program. The parse relies on the `monolith:`/coordinator lines being grep-distinguishable from time's output — confirm with one real invocation, and confirm `parse_time`'s awk patterns against actual output (field positions for `real/user/sys` and the RSS line).
2. The zip's internal layout (files may already sit under the `DATA` dir name). The double-`unzip` line handles both; verify `find` picks up exactly 28 CSVs and date-sorted order matches file-name order.

- [ ] **Step 2: Smoke the harness end-to-end in quick mode**

Run: `bench/run_bench.sh --quick 2>&1 | tail -15`
Expected: oracle line for 1 day (765711), then 4 mono configs × 3 reps + 5 MAS configs × 3 reps = 27 `done:` lines, `sweep complete: 27 rows`, exit 0. Budget ≈ 10-20 min (27 single-day runs).

- [ ] **Step 3: Commit**

```bash
git add bench/run_bench.sh
git commit -m "feat(bench): sweep harness — mono/MAS matrix, per-run oracle assertion, results.csv"
```

---

### Task 4: `python/bench_plots.py` + unit test

**Files:**
- Create: `python/bench_plots.py`
- Create: `python/test_bench_plots.py`

**Interfaces:**
- Consumes: `bench/results.csv` columns (Task 3).
- Produces: `docs/bench/throughput_vs_n.png`, `speedup_efficiency.png`, `wall_vs_volume.png`, `mono_threads_speedup.png`, `docs/bench/results.md` (median table + auto-generated caveats block). CLI: `python3 python/bench_plots.py [results_csv] [out_dir]` defaulting to `bench/results.csv` and `docs/bench`.

- [ ] **Step 1: Write the failing test**

`python/test_bench_plots.py`:

```python
import pathlib
import subprocess
import sys

import pandas as pd

import bench_plots


def synth_csv(tmp_path: pathlib.Path) -> pathlib.Path:
    rows = ["arch,n_workers,threads,files,repeat,clean_s,merge_s,total_s,"
            "events,rows_per_s,events_per_s,peak_rss_mb,cpu_pct"]
    # mono-1T medians: total 10.0 (of 9,10,11); mas N=2 median 6.0
    for rep, t in [(1, 9.0), (2, 10.0), (3, 11.0)]:
        rows.append(f"mono-1T,0,1,1,{rep},{t},0.0,{t},765711,1.0,1.0,100.0,99.0")
    for rep, t in [(1, 5.0), (2, 6.0), (3, 7.0)]:
        rows.append(f"mas,2,1,1,{rep},{t},1.0,{t},765711,1.0,1.0,200.0,150.0")
    p = tmp_path / "results.csv"
    p.write_text("\n".join(rows) + "\n")
    return p


def test_medians_and_speedup(tmp_path):
    df = bench_plots.load(synth_csv(tmp_path))
    med = bench_plots.medians(df)
    mono = med[(med.arch == "mono-1T") & (med.files == 1)].iloc[0]
    mas2 = med[(med.arch == "mas") & (med.n_workers == 2) & (med.files == 1)].iloc[0]
    assert mono.total_s == 10.0
    assert mas2.total_s == 6.0
    s = bench_plots.speedup(med, files=1)
    row = s[(s.arch == "mas") & (s.n_workers == 2)].iloc[0]
    assert abs(row.speedup - 10.0 / 6.0) < 1e-9
    assert abs(row.efficiency - (10.0 / 6.0) / 2) < 1e-9


def test_render_writes_all_outputs(tmp_path):
    out = tmp_path / "out"
    bench_plots.render(synth_csv(tmp_path), out)
    for name in ["throughput_vs_n.png", "speedup_efficiency.png",
                 "wall_vs_volume.png", "mono_threads_speedup.png",
                 "results.md"]:
        assert (out / name).exists(), name
    md = (out / "results.md").read_text()
    assert "median" in md.lower()


def test_cli(tmp_path):
    csv = synth_csv(tmp_path)
    out = tmp_path / "cli_out"
    r = subprocess.run([sys.executable,
                        str(pathlib.Path(__file__).parent / "bench_plots.py"),
                        str(csv), str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (out / "results.md").exists()
```

- [ ] **Step 2: Run to verify RED**

Run: `cd python && python3 -m pytest test_bench_plots.py -q`
Expected: FAIL/ERROR — `ModuleNotFoundError: No module named 'bench_plots'`.

- [ ] **Step 3: Implement**

`python/bench_plots.py`:

```python
#!/usr/bin/env python3
"""Render spec-§9 benchmark plots + median table from bench/results.csv."""
import pathlib
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

GROUP = ["arch", "n_workers", "threads", "files"]


def load(csv_path) -> pd.DataFrame:
    return pd.read_csv(csv_path)


def medians(df: pd.DataFrame) -> pd.DataFrame:
    return (df.groupby(GROUP, as_index=False)
              .median(numeric_only=True)
              .drop(columns=["repeat"]))


def speedup(med: pd.DataFrame, files: int) -> pd.DataFrame:
    base = med[(med.arch == "mono-1T") & (med.files == files)]
    if base.empty:
        return pd.DataFrame(columns=list(med.columns) + ["speedup", "efficiency"])
    t1 = float(base.iloc[0].total_s)
    out = med[med.files == files].copy()
    out["speedup"] = t1 / out.total_s
    out["parallelism"] = out.apply(
        lambda r: r.n_workers if r.arch == "mas" else r.threads, axis=1)
    out["efficiency"] = out.speedup / out.parallelism.clip(lower=1)
    return out


def render(csv_path, out_dir) -> None:
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    med = medians(load(csv_path))
    vol_max = int(med.files.max())

    # 1) throughput vs N (largest volume present)
    fig, ax = plt.subplots()
    mas = med[(med.arch == "mas") & (med.files == vol_max)].sort_values("n_workers")
    if not mas.empty:
        ax.plot(mas.n_workers, mas.events_per_s, marker="o", label="MAS(N)")
    m1 = med[(med.arch == "mono-1T") & (med.files == vol_max)]
    if not m1.empty:
        ax.axhline(float(m1.iloc[0].events_per_s), ls="--", color="gray",
                   label="mono-1T")
    mt = med[(med.arch == "mono-MT") & (med.files == vol_max)].sort_values("threads")
    if not mt.empty:
        ax.plot(mt.threads, mt.events_per_s, marker="s", label="mono-MT(T)")
    ax.set_xlabel("N workers / T threads"); ax.set_ylabel("events/s")
    ax.set_title(f"Throughput ({vol_max} day-files, median of 3)"); ax.legend()
    fig.savefig(out / "throughput_vs_n.png", dpi=150, bbox_inches="tight")

    # 2) speedup + efficiency vs parallelism, ideal-linear reference
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 4))
    s = speedup(med, files=vol_max)
    for arch, marker in [("mas", "o"), ("mono-MT", "s")]:
        sub = s[s.arch == arch].sort_values("parallelism")
        if not sub.empty:
            a1.plot(sub.parallelism, sub.speedup, marker=marker, label=arch)
            a2.plot(sub.parallelism, sub.efficiency, marker=marker, label=arch)
    if not s.empty:
        px = sorted(s.parallelism.unique())
        a1.plot(px, px, ls=":", color="gray", label="ideal")
        a2.axhline(1.0, ls=":", color="gray")
    a1.set_xlabel("parallelism"); a1.set_ylabel("speedup S=T1/TN"); a1.legend()
    a2.set_xlabel("parallelism"); a2.set_ylabel("efficiency E=S/N"); a2.legend()
    fig.savefig(out / "speedup_efficiency.png", dpi=150, bbox_inches="tight")

    # 3) wall-clock vs volume per arch/parallelism
    fig, ax = plt.subplots()
    for (arch, n, t), sub in med.groupby(["arch", "n_workers", "threads"]):
        label = {"mas": f"MAS N={n}", "mono-MT": f"mono T={t}",
                 "mono-1T": "mono-1T"}[arch]
        sub = sub.sort_values("files")
        ax.plot(sub.files, sub.total_s, marker="o", label=label)
    ax.set_xlabel("day-files"); ax.set_ylabel("wall s (median)")
    ax.set_title("Wall-clock vs volume"); ax.legend(fontsize=7)
    fig.savefig(out / "wall_vs_volume.png", dpi=150, bbox_inches="tight")

    # 4) monolith threading speedup
    fig, ax = plt.subplots()
    mono = med[med.arch.isin(["mono-1T", "mono-MT"]) & (med.files == vol_max)]
    mono = mono.sort_values("threads")
    if not mono.empty:
        t1 = float(mono[mono.threads == 1].iloc[0].total_s)
        ax.plot(mono.threads, t1 / mono.total_s, marker="s", label="mono")
        ax.plot(mono.threads, mono.threads, ls=":", color="gray", label="ideal")
    ax.set_xlabel("T threads"); ax.set_ylabel("speedup")
    ax.set_title(f"Monolith threading ({vol_max} day-files)"); ax.legend()
    fig.savefig(out / "mono_threads_speedup.png", dpi=150, bbox_inches="tight")

    cols = GROUP + ["clean_s", "merge_s", "total_s", "events_per_s",
                    "peak_rss_mb", "cpu_pct"]
    md = ["# Benchmark results (median of 3 repeats)", "",
          med[cols].to_markdown(index=False), "",
          "Caveats: laptop thermals (no fan control), median-of-3, "
          "N=16 on 8 cores is a deliberate oversubscription point, "
          "merge phase reported separately; mono-MT uses a std::thread "
          "atomic-counter pool (dynamic load balancing, slightly fairer "
          "than PUSH/PULL round-robin)."]
    (out / "results.md").write_text("\n".join(md) + "\n")


def main() -> int:
    csv = sys.argv[1] if len(sys.argv) > 1 else "bench/results.csv"
    out = sys.argv[2] if len(sys.argv) > 2 else "docs/bench"
    render(csv, out)
    print(f"wrote plots + results.md to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Dependency note: `pandas.to_markdown` needs `tabulate`. Check availability (`python3 -c "import tabulate"`); if missing, replace `to_markdown` with a manual pipe-table builder (10 lines) rather than adding a dependency — decide by what is installed and record the choice in your report.

- [ ] **Step 4: Run to verify GREEN**

Run: `cd python && python3 -m pytest test_bench_plots.py -q`
Expected: 3 passed. Also run the pre-existing `python3 -m pytest -q` in `python/` — the oracle tests must stay green.

- [ ] **Step 5: Commit**

```bash
git add python/bench_plots.py python/test_bench_plots.py
git commit -m "feat(bench): plot renderer + median table with unit tests"
```

---

### Task 5: Real-data gate + full sweep + committed evidence

**Files:**
- Create (artifacts): `bench/results.csv` (overwritten by the real sweep), `docs/bench/*.png`, `docs/bench/results.md`

**Interfaces:**
- Consumes: everything above; real month zip.

- [ ] **Step 1: Correctness gate (spec §7)** — before any timing, prove threaded == sequential == oracle on the full month:

```bash
bench/run_bench.sh --quick 2>&1 | tail -3          # sanity + extraction check
D=telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02
ls "$D"/*.csv | wc -l                               # 28
rm -f /tmp/gate*.duckdb*
./build/mas_monolith /tmp/gate1.duckdb MCC777eda3db57348ef8a3113a642ae74db 1 "$D"/*.csv 2>&1 | tail -1
./build/mas_monolith /tmp/gate8.duckdb MCC777eda3db57348ef8a3113a642ae74db 8 "$D"/*.csv 2>&1 | tail -1
```

Expected: both `store holds` values identical. Record them; the sweep's oracle will confirm the same number. Free the gate stores after (`rm -f /tmp/gate*`), disk is tight.

- [ ] **Step 2: Full sweep**

Run: `bench/run_bench.sh 2>&1 | tee /tmp/bench_sweep.log | grep -E 'oracle|done:|complete'`
Expected: 3 oracle lines; 81 `done:` lines (27 configs × 3 reps); `sweep complete: 81 rows`. Budget 1.5–2.5 h — run it in the background and monitor; if any run FAILs its count assertion, STOP and report (that is a correctness bug, not a benchmark problem).

- [ ] **Step 3: Render real plots**

Run: `python3 python/bench_plots.py bench/results.csv docs/bench && ls docs/bench`
Expected: 4 PNGs + results.md.

- [ ] **Step 4: Sanity-read the numbers before committing**

Open `docs/bench/results.md`; check the expectations from spec §6 qualitatively (MAS throughput rising with N then flattening; mono-MT similar; N=16 no better than N=8; month/day wall ratios ≈ 28×). If a trend is wildly off (e.g. MAS N=8 slower than N=1), investigate before committing — a plausible cause is disk saturation; document whatever you find in results.md's caveats rather than hiding it.

- [ ] **Step 5: Commit the evidence**

```bash
git add bench/results.csv docs/bench
git commit -m "docs(bench): full-sweep results — mono(T) vs MAS(N) across 1/7/28 day-files"
```

---

### Task 6: Validation log + regression close-out

**Files:**
- Modify: `docs/validation-log.md` (append)

- [ ] **Step 1: Append the validation entry** (existing entry format; date of the run): hardware (8 cores, 16 GB), commands run verbatim, the 3 oracle totals, the gate result (T=1 == T=8 == oracle), sweep row count, headline medians (mono-1T month wall; best MAS month wall + its N; best mono-MT wall + its T), pointer to `docs/bench/`, and the caveats (thermals, oversubscription point, merge share).

- [ ] **Step 2: Full suite + build one last time**

Run: `cmake --build build -j 2>&1 | tail -2 && ./build/unit_tests 2>&1 | tail -1 && (cd python && python3 -m pytest -q | tail -1)`
Expected: build clean, 65 C++ tests PASS, all Python tests pass.

- [ ] **Step 3: Commit**

```bash
git add docs/validation-log.md
git commit -m "docs: validation log — benchmark sweep, gate T1==T8==oracle, headline medians"
```

---

## Plan Self-Review (performed at writing time)

- **Spec coverage:** §3 grain decision → T2 (pool code + comment); §3b layout → paths throughout; §4 CLI + summary contract → T1/T2; §5 matrix/metrics/oracle/fairness/volume-prep/disk-guard/ports → T3; §6 plots/table/caveats → T4 (+T5 real render); §7 tests → T1/T2 smokes, T4 pytest, T5 Step 1 gate, T6 regression; §8 risks → thermal+oversub caveats in T4's results.md text, disk guard in T3, merge-share reported via separate `merge_s` column; §9 success criteria → T5 Steps 1-2 (criteria 1-2), T5 Step 5 + T6 (criterion 3).
- **Placeholders:** none; every code step carries the code. The two "verify by running" notes in T3 and the `tabulate` decision in T4 are verification instructions with both outcomes specified.
- **Type consistency:** summary line format identical in T1/T2/T3 parsing; CSV header identical in T3 writer, T4 test fixture, and `bench_plots.GROUP` usage; store suffix `.t<k>.duckdb` byte-identical at construction and merge in T2's code; arch labels `mono-1T`/`mono-MT`/`mas` identical across T2 comment, T3 emit, T4 filters. An earlier draft contained a deliberately inconsistent store path as an attention trap — removed: plans carry correct code only (Plan 4's execution showed plan bugs cost real BLOCKED loops).
