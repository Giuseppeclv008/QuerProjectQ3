#!/usr/bin/env bash
# What did the disk-forced per-day regime cost the write measurement?
#
# The superseded fallback sweep ran 28 mas_monolith invocations per backend, so
# each backend paid 28 process starts -- and DuckDB paid 28 opens and checkpoints
# of a growing store, which Parquet has no equivalent of.
#
# NOTE: this script's first two runs, taken with ~630 MB free, appeared to show
# a 1.26-1.28x penalty for DuckDB's single invocation. Repeated with 2.3 GB
# free it vanished (0.99-1.00x): it was measuring free space, not invocation
# count. Both pairs are in calibrate.out. Run this only on a volume with room.
#
# So: run the same four days BOTH ways -- four invocations, then one -- and
# report the difference. Four days, not the full month, because there is not
# enough free disk for a second month-scale store.
set -euo pipefail

ROOT="/Users/giuseppecalvello/dev/Polito/magistrale /secondosemestre20262027/System and Device Programming/Quer project"
cd "$ROOT"

ZIP="telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02.zip"
STAGE="/tmp/t6-cal"
DAYS="01 02 03 04"

rm -rf "$STAGE"; mkdir -p "$STAGE"
CSVS=()
for d in $DAYS; do
    csv="telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02-${d}.csv"
    unzip -qo "$ZIP" "$csv" -d "$STAGE"
    CSVS+=("$STAGE/$csv")
done

timed() {            # timed <label> <cmd...>
    local label="$1"; shift
    local err; err="$(mktemp)"
    /usr/bin/time -p "$@" >/dev/null 2>"$err"
    printf '%-28s real %ss   (%s)\n' "$label" \
        "$(sed -n 's/^real  *//p' "$err")" \
        "$(grep -o 'total [0-9.]* s' "$err" | tail -1)"
    rm -f "$err"
}

for backend in parquet duckdb; do
    echo "--- $backend ---"

    # N invocations, one per day: the regime the month sweep was forced into.
    rm -rf "$STAGE/pq-n"; mkdir -p "$STAGE/pq-n"
    rm -f "$STAGE/duck-n.duckdb" "$STAGE/duck-n.duckdb.wal"
    for c in "${CSVS[@]}"; do
        if [ "$backend" = parquet ]; then
            timed "  per-day $(basename "$c" | tail -c 7)" \
                ./build/mas_monolith --format parquet "$STAGE/pq-n" MCC 1 "$c"
        else
            timed "  per-day $(basename "$c" | tail -c 7)" \
                ./build/mas_monolith "$STAGE/duck-n.duckdb" MCC 1 "$c"
        fi
    done

    # One invocation over all four: the regime the plan asked for.
    rm -rf "$STAGE/pq-1"; mkdir -p "$STAGE/pq-1"
    rm -f "$STAGE/duck-1.duckdb" "$STAGE/duck-1.duckdb.wal"
    if [ "$backend" = parquet ]; then
        timed "  single invocation" \
            ./build/mas_monolith --format parquet "$STAGE/pq-1" MCC 1 "${CSVS[@]}"
    else
        timed "  single invocation" \
            ./build/mas_monolith "$STAGE/duck-1.duckdb" MCC 1 "${CSVS[@]}"
    fi

    rm -rf "$STAGE/pq-n" "$STAGE/pq-1"
    rm -f "$STAGE/duck-n.duckdb" "$STAGE/duck-n.duckdb.wal" \
          "$STAGE/duck-1.duckdb" "$STAGE/duck-1.duckdb.wal"
    df -h . | tail -1
done

rm -rf "$STAGE"
