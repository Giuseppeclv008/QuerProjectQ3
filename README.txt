PROJECT Q3 - MULTI-AGENT SYSTEM FOR INDUSTRIAL IOT DATA REFINEMENT
==================================================================

The programs read the raw 1 Hz telemetry day-files of an AROL Equatorque
capping machine (36 capping heads), remove the duplicate polls and write one
row per real cap event. A Python layer then builds reports from the cleaned
data. Design and measurements: DOCUMENTATION.md.


1. REQUIREMENTS
---------------

  - CMake >= 3.16 and a C++20 compiler (gcc 12+, clang 14+, MSVC 2022)
  - Internet access at the first configure: CMake downloads DuckDB 1.2.2,
    libzmq 4.3.5 and GoogleTest 1.14.0
  - Optional: CUDA Toolkit (GPU cleaning engine)
  - Optional: Python 3 (validation oracle, benchmarks, analytics)


2. COMPILE
----------

  cmake -B build -DCMAKE_BUILD_TYPE=Release
  cmake --build build --parallel

Binaries are created in build/ (build/Release/ with the MSVC generator).

CMake options (ON/OFF, defaults in brackets):

  MAS_ENABLE_CUDA  [OFF]  build mas_cuda_clean and the --engine=cuda path
  MAS_ENABLE_ZMQ   [ON]   build mas_coordinator and mas_worker
  MAS_BUILD_TESTS  [ON]   build the unit tests (needs GoogleTest)
  MAS_BENCH_ONLY   [OFF]  build only clean core + bench binaries; downloads
                          nothing, builds no tests

Run the unit tests:

  cd build && ctest -C Release --output-on-failure


3. DATASET FORMAT
-----------------

Input: one CSV file per day, taken from the month archives
telemetry_<machine_id>_<YYYY-MM>.zip. The data is not part of the repository.

  - comma separated, one header line, LF or CRLF line ends
  - exactly 109 columns, in this order:
        timestamp
        H01 Count ... H36 Count            (cap counter of each head)
        H01 AppTorque ... H36 AppTorque    (applied torque)
        H01 Status ... H36 Status          (closure status, bitmask;
                                            bit 0 set = cap rejected)
  - one row per second (about 86,400 rows, 58 MB per day)
  - timestamp is text, e.g. 2026-02-01T00:00:00.000
  - a header that differs from the one above is an error; a row with a wrong
    number of fields or an unreadable/out-of-range number is skipped and
    counted

Output: one row per cap event.

  machine_id, head_id, ts, cap_seq, app_torque, status, delta,
  is_fault, aggregated, reset

  cap_seq     counter value of the head at that cap
  delta       caps since the previous row (> 1 means aggregated = true)
  reset       the counter went down (PLC reset); delta is 0
  is_fault    the reject bit of status is set

A row is produced when a head's counter changes between two consecutive
rows; unchanged counters produce nothing.

Output formats:
  .csv      header as above
  .duckdb   table cap_events, same columns ("reset" is called is_reset);
            (machine_id, head_id, ts) is unique, so running the same file
            twice adds no rows
  Parquet   same columns as the DuckDB table (see --format below)


4. RUN
------

machine_id is a label for the machine, stored in every event. It is
required. The examples use MCC and the data files telemetry_*/*2026-02-01.csv.

4.1 clean - one day-file

  build/clean [--format duckdb|parquet] <raw_in.csv> <out> <machine_id>

  <out>   *.duckdb -> DuckDB store; any other name -> CSV file;
          with --format parquet <out> is a directory and the file
          <out>/<input name>.parquet is written

  build/clean telemetry_*/*2026-02-01.csv events.csv MCC
  build/clean telemetry_*/*2026-02-01.csv events.duckdb MCC

4.2 mas_monolith - several files, threads in one process

  build/mas_monolith [--no-store] [--engine=cpu|cuda] [--format duckdb|parquet]
                     <out.duckdb|out_dir> <machine_id> <threads>
                     <day1.csv> [day2.csv ...]

  <threads>     1 = sequential; > 1 = one day-file per thread, one private
                store per thread, merged into <out> at the end
  --no-store    count the events and discard them (needs threads = 1)
  --engine      cleaning engine, default cpu; cuda needs a build with
                MAS_ENABLE_CUDA=ON and threads = 1; no fallback to cpu
  --format      parquet writes Parquet files into the directory <out_dir>

  build/mas_monolith unified.duckdb MCC 4 telemetry_*/*.csv

4.3 mas_worker + mas_coordinator - several processes (ZeroMQ)

  build/mas_worker [--format duckdb|parquet] <work_endpoint> <result_endpoint>
                   <hb_endpoint> <out.duckdb|out_dir> <worker_id> <machine_id>

  build/mas_coordinator <work_endpoint> <result_endpoint> <hb_endpoint>
                        [--workers N] <day1.csv> [day2.csv ...]

  The three endpoints are the same for all processes, e.g. tcp://127.0.0.1:5591,
  5592 and 5593. Start the workers first (each one writes its own store),
  then the coordinator; --workers N makes it wait for N workers before it
  dispatches. A worker that stays silent for 30 s is declared dead and its
  files are given to the others. Afterwards merge the worker stores:

  EP="tcp://127.0.0.1:5591 tcp://127.0.0.1:5592 tcp://127.0.0.1:5593"
  build/mas_worker $EP w1.duckdb w1 MCC &
  build/mas_worker $EP w2.duckdb w2 MCC &
  build/mas_coordinator $EP --workers 2 \
      telemetry_*/*2026-02-01.csv telemetry_*/*2026-02-02.csv
  build/mas_merge unified.duckdb MCC w1.duckdb w2.duckdb

4.4 mas_merge and mas_export

  build/mas_merge <dst.duckdb> <machine_id> <src1.duckdb> [src2.duckdb ...]
      merges stores into dst; a corrupt source is skipped with a warning

  build/mas_export <store.duckdb> <out.parquet> [--since TS] [--until TS]
      writes cap_events to Parquet; the store is opened read-only; a bare
      date as --until covers the whole day

4.5 Benchmark programs

  build/bench_cpu <threads> <day1.csv> [day2.csv ...]
      cleaning only, no store (CPU contender of the CUDA benchmark)
  build/mas_cuda_clean [--verify] <day1.csv> [day2.csv ...]
      GPU cleaning (MAS_ENABLE_CUDA=ON); --verify compares the result with
      the CPU code and exits non-zero on any difference

mas_monolith, bench_cpu and mas_cuda_clean also print a "metrics:" line on
stderr with wall time, CPU time and peak memory. A usage error exits with
code 2.


5. CHECK AGAINST THE PYTHON ORACLE
----------------------------------

  python3 python/oracle.py telemetry_*/*2026-02-01.csv
      prints the number of events found by the independent Python version

  python3 python/validate_real.py telemetry_*/*2026-02-01.csv events.csv
      compares every field of every event of events.csv with the oracle


6. ANALYTICS AND REPORTS (Python)
---------------------------------

  python3 -m venv .venv
  .venv/bin/pip install -r python/requirements.txt

  scripts/build_store.sh <out.duckdb> [month.zip ...]
      unzips the month archives and cleans all day-files into one store
      (uses build/mas_monolith with 4 threads, machine_id MCC)

Write a configuration file, for example arol.json:

  { "store_path": "events_3mo.duckdb", "machine_id": "MCC" }

  scripts/arol report kpi       --period 2026-02          --config arol.json
  scripts/arol report drift     --period 2026-02..2026-04 --config arol.json
  scripts/arol report anomalies --period 2026-02          --config arol.json
  scripts/arol ask "<question>" --period 2026-02          --config arol.json

  --config FILE   JSON configuration (store_path, machine_id, torque_min,
                  torque_max, mad_k, idle_min_seconds, idle_max_gap_seconds,
                  provider, model, planning)
  --out DIR       output directory (default: reports)
  --period P      YYYY-MM or YYYY-MM..YYYY-MM (default: whole store)
  --pdf           also export a PDF (needs WeasyPrint)
  --provider P    anthropic or ollama      (ask only)
  --model NAME    model name               (ask only)
  --planning T    plan, select or classify (ask only)

The three "report" commands use no model and need no network. "ask" uses
Claude (set ANTHROPIC_API_KEY) or a local Ollama model (--provider ollama);
without a model it falls back to keyword routing.
Each report is a directory with report.md, report.html, trace.json and PNGs.

Python tests: cd python && ../.venv/bin/python -m pytest -q


7. REPRODUCE THE MEASUREMENTS
-----------------------------

  bench/run_bench.sh [--quick]
      CPU sweep: monolith 1/2/4/8 threads, MAS 1/2/4/8/16 workers, volumes
      of 1, 7 and 28 day-files, 3 repeats. Expects the February 2026 archive
      named in the script, and macOS or Windows (it needs BSD time or
      win_time). Writes bench/results.csv. --quick runs 1 day only.

  python bench/run_bench_cuda.py --data <month.zip or extracted dir> [--quick]
      Python vs C++ vs CUDA cleaning. Build first with:
        cmake -S . -B build-bench -DMAS_BENCH_ONLY=ON -DMAS_ENABLE_CUDA=ON
        cmake --build build-bench --config Release
        pip install -r bench/requirements-bench.txt
      Writes bench/results_cuda.csv and bench/results_cuda_stages.csv.
      Details: bench/README.md.

  python python/bench_plots.py [--cuda]
      redraws the plots in docs/bench/ from the CSV files
