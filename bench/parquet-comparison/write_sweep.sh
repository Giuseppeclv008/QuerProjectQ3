#!/usr/bin/env bash
# Task 6 month-scale write sweep, disk-constrained.
#
# The plan's Step 1 runs one mas_monolith invocation per backend over all 28
# extracted day-files. That needs the 1.5 GB CSV pool and a DuckDB store that
# measured 1183 MB on disk at once; this machine has 652 MB free. So each day is
# extracted from the zip, fed to BOTH backends, and deleted -- peak footprint
# ~57 MB of CSV instead of 1.5 GB.
#
# Both backends run under the identical regime, so the comparison between them
# is fair. What the regime adds -- 28 process starts, 28 DuckDB opens and
# checkpoints against a growing file -- is measured separately by
# task6_calibrate.sh, not assumed to be negligible.
set -euo pipefail

ROOT="/Users/giuseppecalvello/dev/Polito/magistrale /secondosemestre20262027/System and Device Programming/Quer project"
cd "$ROOT"

ZIP="telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02.zip"
STAGE="/tmp/t6-stage"
PQ="/tmp/pq-month"
DUCK="/tmp/duck-month.duckdb"
LOG="${1:?usage: task6_sweep.sh <out.csv>}"

rm -rf "$STAGE" "$PQ"; mkdir -p "$STAGE" "$PQ"
rm -f "$DUCK" "$DUCK".wal "$DUCK".t*.duckdb "$DUCK".t*.duckdb.wal

echo "day,backend,wall_s,reported_total_s,events" > "$LOG"

# The shell's SECONDS builtin is integer-only, which would quantise a 1.2 s
# parquet day to "1" and lose the comparison. /usr/bin/time -p reports real
# time to 0.01 s; its output goes to stderr, so stdout stays the monolith line.
run_one() {          # run_one <day> <backend> <cmd...>
    local day="$1" backend="$2"; shift 2
    # mas_monolith logs its summary to stderr, where /usr/bin/time also writes,
    # so one capture holds both.
    local err; err="$(mktemp)"
    /usr/bin/time -p "$@" >/dev/null 2>"$err"
    local out; out="$(grep '^monolith:' "$err" || true)"
    local wall; wall="$(sed -n 's/^real  *//p' "$err")"
    rm -f "$err"
    # monolith line: "monolith: 1 files, N events, clean X s, merge Y s, total Z s, ..."
    local ev tot
    ev="$(sed -n 's/.*files, \([0-9]*\) events.*/\1/p' <<<"$out")"
    tot="$(sed -n 's/.*total \([0-9.]*\) s.*/\1/p' <<<"$out")"
    [ -n "$ev" ] || { echo "PARSE FAIL day=$day backend=$backend: $out" >&2; exit 1; }
    echo "$day,$backend,$wall,$tot,$ev" >> "$LOG"
    echo "  $backend day=$day wall=${wall}s reported=${tot}s events=$ev"
}

for d in $(seq -w 1 28); do
    csv="telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02-${d}.csv"
    unzip -qo "$ZIP" "$csv" -d "$STAGE"
    [ -f "$STAGE/$csv" ] || { echo "MISSING $csv in $ZIP" >&2; exit 1; }
    echo "day $d"
    run_one "$d" parquet ./build/mas_monolith --format parquet "$PQ" MCC 1 "$STAGE/$csv"
    run_one "$d" duckdb  ./build/mas_monolith "$DUCK" MCC 1 "$STAGE/$csv"
    rm -f "$STAGE/$csv"
    df -h . | tail -1
done

rmdir "$STAGE"
echo "=== totals ==="
awk -F, 'NR>1 {w[$2]+=$3; r[$2]+=$4; e[$2]+=$5}
         END {for (b in w) printf "%-8s wall %7.1fs  reported %7.1fs  events %d\n", b, w[b], r[b], e[b]}' "$LOG"
du -sh "$PQ" "$DUCK"
