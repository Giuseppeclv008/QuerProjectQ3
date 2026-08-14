#!/usr/bin/env bash
# The month-scale write comparison, one mas_monolith invocation per backend.
#
# This is the measurement the plan asked for. An earlier attempt could not run
# it -- 652 MB free would not hold the 1.5 GB CSV pool and the 1.2 GB DuckDB
# store at once -- and fell back to 28 invocations per backend. Once APFS
# released purgeable space the honest version became possible, so it replaces
# the workaround rather than sitting beside it.
#
# usage: write_sweep.sh <out.txt>
set -euo pipefail

ROOT="/Users/giuseppecalvello/dev/Polito/magistrale /secondosemestre20262027/System and Device Programming/Quer project"
cd "$ROOT"

ZIP="telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02.zip"
POOL="telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02"
PQ="/tmp/pq-month"
DUCK="/tmp/duck-month.duckdb"
OUT="${1:?usage: write_sweep.sh <out.txt>}"

exec > >(tee "$OUT") 2>&1
echo "# one invocation per backend, 28 day-files, machine_id MCC, 1 thread"
echo "# $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Both stores are rebuilt from nothing. mas_monolith opens a DuckDB store in
# append mode, and a stale .wal or a leftover per-thread store beside it folds a
# previous run's rows into this one -- the failure scripts/build_store.sh
# documents. Parquet needs the same care for a different reason: the directory
# is globbed at read time, so an orphan .parquet from an older run is read as
# data.
rm -rf "$PQ"; mkdir -p "$PQ"
rm -f "$DUCK" "$DUCK".wal "$DUCK".t*.duckdb "$DUCK".t*.duckdb.wal

if ! compgen -G "$POOL/*.csv" >/dev/null 2>&1; then
    echo "extracting $ZIP"
    mkdir -p "$POOL"
    unzip -qo "$ZIP" -d "$POOL"
fi
CSVS=("$POOL"/*.csv)
echo "input: ${#CSVS[@]} day-files"
[ "${#CSVS[@]}" -eq 28 ] || { echo "expected 28 day-files, got ${#CSVS[@]}"; exit 1; }
df -h . | tail -1

# Order is a variable here, not an accident: this volume slows DuckDB's larger
# sequential writes when it runs low, and each backend's store eats into the
# free space the next one sees. The first run took Parquet first (2.1 GiB free)
# and DuckDB second (812 MiB) -- exactly the direction that flatters Parquet.
# ORDER=duckdb-first counterbalances it.
ORDER="${ORDER:-parquet-first}"
case "$ORDER" in
  parquet-first) BACKENDS="parquet duckdb" ;;
  duckdb-first)  BACKENDS="duckdb parquet" ;;
  *) echo "ORDER must be parquet-first or duckdb-first" >&2; exit 2 ;;
esac
echo "# order: $ORDER"

for backend in $BACKENDS; do
    echo "=== $backend ==="
    if [ "$backend" = parquet ]; then
        /usr/bin/time -p ./build/mas_monolith --format parquet "$PQ" MCC 1 "${CSVS[@]}"
    else
        /usr/bin/time -p ./build/mas_monolith "$DUCK" MCC 1 "${CSVS[@]}"
    fi
    df -h . | tail -1
done

echo "=== stores ==="
du -sk "$PQ" "$DUCK"
