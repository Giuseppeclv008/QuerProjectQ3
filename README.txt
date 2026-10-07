Q3 - AROL capping machine: telemetry cleaning and reports
=========================================================

The capping machine logs one row per second, so the same cap appears many
times in the raw data. These programs keep one row per cap actually applied,
and a Python tool then builds reports from the cleaned data.

Design and measurements: DOCUMENTATION.md.


1. BUILD (once)
---------------

You need CMake 3.16+, a C++20 compiler (gcc 12+, clang 14+ or Visual Studio
2022) and an internet connection: the first run downloads DuckDB, libzmq and
GoogleTest.

  cmake -B build -DCMAKE_BUILD_TYPE=Release
  cmake --build build --parallel

Visual Studio on Windows: add --config Release to the second command.

The programs appear in build/ (Windows: build/Release/). In the rest of this
file, "build/" means that folder.

Optional switches, added to the first command:

  -DMAS_ENABLE_CUDA=ON    GPU cleaning (needs the CUDA Toolkit)       [OFF]
  -DMAS_ENABLE_ZMQ=OFF    do not build mas_worker / mas_coordinator   [ON]
  -DMAS_BUILD_TESTS=OFF   do not build the unit tests                 [ON]

Check the build (optional):  cd build && ctest -C Release --output-on-failure


2. THE PROGRAMS
---------------

  clean            cleans ONE day-file
  mas_monolith     cleans MANY day-files in one process (start here for a month)
  mas_worker       with mas_coordinator: many day-files, many processes
  mas_coordinator    (optional, see 3.4)
  mas_merge        joins several .duckdb stores into one
  mas_export       writes a store out as one Parquet file

Run everything from the repository folder. Every program prints its messages
on stderr, and prints its usage line if you run it with no arguments.

<machine_id> is a label stored in every row (for example MCC). It is always
required: the programs never guess it. Use the same label later in the reports.


3. RUN
------

3.1  One day

  build/clean telemetry_MCC_2026-02-01.csv events.csv MCC

  prints, for example:  wrote 765711 cap events

  The output type follows its name:
    events.csv       a CSV file (overwritten if it exists)
    events.duckdb    a DuckDB store; running the same file again adds nothing
    --format parquet <folder>   one Parquet file per input, named after it

    build/clean --format parquet telemetry_MCC_2026-02-01.csv out_parquet MCC

  Options go BEFORE the file names.

3.2  Many days

  build/mas_monolith unified.duckdb MCC 4 telemetry_MCC_2026-02-*.csv
                     ^output        ^id ^threads ^day-files

  <threads>  1 = one file after another, N = N files at the same time.
  The store keeps growing: new days are added, days already in it are not
  duplicated. To start from scratch, delete the .duckdb file first.

  At the end it prints two lines, for example:
    monolith: 28 files, 21872663 events, clean 12.3 s, merge 1.2 s, total 13.5 s,
              store holds 21872663 rows, engine cpu

  The * is expanded by the shell: fine in Git Bash, macOS and Linux. In cmd or
  PowerShell, list the files by name.

  Straight from the month archives (unzips them, then cleans with 4 threads,
  label MCC; an existing output file is deleted first):

  scripts/build_store.sh events_3mo.duckdb telemetry_*.zip

3.3  Join stores, export

  build/mas_merge unified.duckdb MCC march.duckdb
      adds the rows of march.duckdb to unified.duckdb. Rows keep the label
      they already had. A source that cannot be read is skipped with a warning.

  build/mas_export unified.duckdb events.parquet --since 2026-02-03 --until 2026-02-03
      reads the store (never changes it) and writes events.parquet.
      --since / --until are optional and inclusive; a bare date as --until
      covers that whole day.

3.4  Many processes (optional)

  Start the workers first, then the coordinator, then merge their stores:

  EP="tcp://127.0.0.1:5591 tcp://127.0.0.1:5592 tcp://127.0.0.1:5593"
  build/mas_worker $EP w1.duckdb w1 MCC &
  build/mas_worker $EP w2.duckdb w2 MCC &
  build/mas_coordinator $EP --workers 2 telemetry_MCC_2026-02-01.csv telemetry_MCC_2026-02-02.csv
  build/mas_merge unified.duckdb MCC w1.duckdb w2.duckdb

  The three endpoints must be identical in every command. Each worker needs
  its own name (w1, w2, ...) and its own store. --workers N makes the
  coordinator wait for N workers before it hands out files. A worker silent
  for 30 s is declared dead and its files go to the others.

  You see: dispatched 2 files: 2 ok, 0 failed, <N> events, 0 workers died

3.5  Check a result (optional; plain Python 3, no extra packages)

  python3 python/oracle.py telemetry_MCC_2026-02-01.csv
      event count found by an independent Python version: it must match.
  python3 python/validate_real.py telemetry_MCC_2026-02-01.csv events.csv
      compares every field of every event with the Python version.


4. REPORTS
----------

Set up once. The scripts need bash: on Windows use Git Bash.

  python3 -m venv .venv
  .venv/bin/pip install -r python/requirements.txt      (Windows: .venv/Scripts/pip)

(The pinned versions in python/requirements.txt were tested on Python 3.14.)

Tell the tool where the store is. Save this as arol.json:

  { "store_path": "unified.duckdb", "machine_id": "MCC" }

Then (./arol.json is read automatically; --config FILE picks another):

  scripts/arol report kpi       --period 2026-02
  scripts/arol report drift     --period 2026-02..2026-04
  scripts/arol report anomalies --period 2026-02

  kpi        success rate, production speed, idle time, per head
  drift      how torque and success rate move over time, head by head
  anomalies  readings outside the torque band, unusual heads, rejected caps

Each command prints a folder (reports/kpi, reports/drift, ...) containing:
  report.md      the report (source of truth)
  report.html    the same, one portable file with the plots inside
  trace.json     every analysis that was run, with its arguments
  *.png          the plots

The three report commands use no model: same store and period, same report.

Ask a question in plain English (a model picks which analyses to run):

  scripts/arol ask "which head behaves differently, and why?" --period 2026-02

  Local model (default): ollama serve && ollama pull qwen2.5:7b
  Hosted model:  export ANTHROPIC_API_KEY=...
                 add   --provider anthropic --model claude-opus-5
  The numbers always come from the analyses, never from the model. With no key
  or no model reachable, ask falls back to a keyword router and the report
  says so. Answers go to reports/ask/<timestamp>/.


5. PARAMETERS
-------------

clean [--format duckdb|parquet] <input.csv> <output> <machine_id>

  input        one raw day-file
  output       *.duckdb = store; any other name = CSV file;
               with --format parquet = folder
  --format     duckdb (default) or parquet

mas_monolith [options] <output> <machine_id> <threads> <day1.csv> [day2.csv ...]

  output       .duckdb file, or a folder with --format parquet
  threads      1 or more
  --format     duckdb (default) or parquet
  --engine     --engine=cpu (default) or --engine=cuda (CUDA build only)
  --no-store   count the events and write nothing (<output> is still required)

  Refused (exit 2):
    --no-store or --engine=cuda with threads above 1
    --no-store together with --format parquet
    an option placed after the file names
    --format parquet when two input files have the same name

mas_worker [--format duckdb|parquet] <work_ep> <result_ep> <hb_ep> <output> <worker_id> <machine_id>
mas_coordinator <work_ep> <result_ep> <hb_ep> [--workers N] <day1.csv> [day2.csv ...]

  *_ep         the three ZeroMQ endpoints, e.g. tcp://127.0.0.1:5591 (same everywhere)
  worker_id    unique name of this worker
  output       the worker's own .duckdb file (a folder with --format parquet)

mas_merge <dst.duckdb> <machine_id> <src1.duckdb> [src2.duckdb ...]

mas_export <store.duckdb> <out.parquet> [--since TS] [--until TS]

arol report <kpi|drift|anomalies> [options]
arol ask "<question>" [options]

  --config FILE   JSON settings (below). Without it: ./arol.json if present,
                  else store events.duckdb in the current folder and the
                  default values
  --period P      YYYY-MM or YYYY-MM..YYYY-MM. Without it: the whole store
  --out DIR       where report folders are written [reports]
  --pdf           also write a PDF (needs WeasyPrint)
  -v              more detail on screen
  Only used by ask:
  --provider      ollama (default) or anthropic
  --model         model name, e.g. qwen2.5:7b
  --planning      plan (the model composes the analyses, default),
                  select (it only picks tools) or classify (it picks one report)

arol.json (all keys are optional; defaults in brackets)

  store_path            the .duckdb store                         [events.duckdb]
  machine_id            the label used when cleaning              [MCC]
  torque_min/torque_max expected torque band, Nm                  [1.5 / 2.5]
  mad_k                 a reading this many robust sigmas from
                        the head's median counts as unusual       [3.0]
  idle_min_seconds      no-load for longer than this = idle       [300]
  idle_max_gap_seconds  a hole in the data longer than this ends
                        an idle period                            [600]
  provider, model       model used by ask       [ollama, qwen2.5:7b]
  ollama_host, num_ctx  local model address and context size
                                              [http://localhost:11434, 8192]

  An unknown key, or a value of the wrong type, is an error (exit 2) before
  any work starts.


6. DATA FORMATS
---------------

INPUT: raw day-file
  one CSV per day, e.g. telemetry_MCC_2026-02-01.csv, taken from the month
  archive telemetry_<machine>_<YYYY-MM>.zip. The data is not in the repository.

  - comma separated, one header line, LF or CRLF line ends
  - 109 columns, in this order, with exactly these names:
        timestamp
        H01 Count ... H36 Count            cap counter of each head
        H01 AppTorque ... H36 AppTorque    applied torque, Nm
        H01 Status ... H36 Status          closure status; bit 0 set (an odd
                                           number, e.g. 65) = cap rejected
  - one row per second, about 86,400 per day
  - timestamp is text like 2026-02-01T00:00:00.000. Keep this form: row order
    is checked on the text

  What happens with bad input:
    a different header      the file is refused
    a bad row               skipped and counted: "skipped N malformed rows"
                            (wrong number of fields, or a value that is not a
                            number or is out of range)
    time going backwards    the row is kept but counted: "N out-of-order
                            timestamps"

OUTPUT: one row per cap event
  A row is written when a head's counter changes between two consecutive rows.

  machine_id   the label you gave
  head_id      1..36
  ts           timestamp of the row where the change was seen
  cap_seq      the head's counter value at that cap
  app_torque   applied torque in that row
  status       closure status in that row
  delta        caps since the previous row (normally 1)
  is_fault     1 if the cap was rejected (status odd)
  aggregated   1 if delta is above 1: several caps fell between two rows
  reset        1 if the counter went down (PLC reset). delta is 0 then

  Three file types, same columns:
    .csv       header as above; booleans written as 0 / 1
    .duckdb    table cap_events. "reset" is called is_reset. Types: head_id
               small integer, ts TIMESTAMP, the three flags BOOLEAN.
               (machine_id, head_id, ts) is unique, which is why running the
               same file twice adds no rows
    Parquet    the columns of the DuckDB table, one file per day-file in a
               folder, named after the input (telemetry_MCC_2026-02-01.parquet)

EXAMPLE
  Input, heads 1 and 2 only (the real file has 36 heads in each group):

  timestamp                H01 Count  H02 Count  H01 AppTorque  H02 AppTorque  H01 Status  H02 Status
  2026-02-01T00:00:00.000  100        50         2.5            2.25           0           0
  2026-02-01T00:00:01.000  100        50         2.5            2.25           0           0
  2026-02-01T00:00:02.000  101        50         2.5            2.25           0           0
  2026-02-01T00:00:03.000  101        53         2.5            2.25           0           65
  2026-02-01T00:00:04.000  0          53         2.5            2.25           0           65

  Output of  build/clean <that file> events.csv MCC :

  machine_id,head_id,ts,cap_seq,app_torque,status,delta,is_fault,aggregated,reset
  MCC,1,2026-02-01T00:00:02.000,101,2.5,0,1,0,0,0
  MCC,2,2026-02-01T00:00:03.000,53,2.25,65,3,1,1,0
  MCC,1,2026-02-01T00:00:04.000,0,2.5,0,0,0,0,1

  Five rows, three events:
    00:00:00  first row of the file: counters are only remembered. No event
    00:00:01  nothing changed (a repeated poll). No event
    00:00:02  head 1: 100 -> 101. One cap
    00:00:03  head 2: 50 -> 53. Three caps in one row: delta 3, aggregated 1.
              Status 65 is odd, so is_fault 1
    00:00:04  head 1: 101 -> 0. PLC reset: reset 1, delta 0


7. WHEN SOMETHING GOES WRONG
----------------------------

Exit codes of the C++ programs:
  0   done
  1   cleaning or writing failed
  2   the command line is wrong; the usage line is printed.
      mas_monolith also uses 2 when an input file cannot be opened or has a
      wrong header

scripts/arol:
  0   done. A period with no data is not an error: the report says so
  1   every analysis step failed (for example --period February)
  2   bad option or bad arol.json, found before any work starts

Two slips behind most "usage" errors: a missing <machine_id>, and an option
placed after the file names (options go before them).
