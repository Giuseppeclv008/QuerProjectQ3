#!/usr/bin/env bash
# Chaos E2E (resilience spec §10): run 2 workers on real day-files, kill -9
# one mid-run, assert the run completes and merged counts match the oracle.
# usage: scripts/chaos_e2e.sh <day1.csv> <day2.csv> [day3.csv ...]
set -euo pipefail

[ $# -ge 2 ] || { echo "usage: $0 <day1.csv> <day2.csv> [more.csv ...]"; exit 2; }
BUILD="${BUILD_DIR:-build}"
for exe in mas_coordinator mas_worker mas_merge clean; do
    [ -x "$BUILD/$exe" ] || { echo "missing $BUILD/$exe (build first)"; exit 2; }
done

T="$(mktemp -d /tmp/mas_chaos.XXXXXX)"
PIDS=()
cleanup() {
    for p in "${PIDS[@]:-}"; do kill -9 "$p" 2>/dev/null || true; done
    rm -rf "$T"
}
trap cleanup EXIT

WORK=tcp://127.0.0.1:5571 RES=tcp://127.0.0.1:5572 HB=tcp://127.0.0.1:5573

# Oracle: single-process count per file (clean CLI validated vs the Python
# oracle in Plans 1-2), summed. The count is on stderr as
# "wrote <n> cap events; store now holds <m> rows" (stdout stays empty).
EXPECTED=0
for f in "$@"; do
    rm -f "$T/oracle.duckdb" "$T/oracle.duckdb.wal"
    n="$("$BUILD/clean" "$f" "$T/oracle.duckdb" 2>&1 >/dev/null \
         | sed -n 's/^wrote \([0-9][0-9]*\) cap events.*/\1/p')" \
        || { echo "oracle clean failed for $f"; exit 1; }
    case "$n" in (*[!0-9]*|"") echo "oracle count failed for $f: '$n'"; exit 1;; esac
    EXPECTED=$((EXPECTED + n))
done
echo "oracle total: $EXPECTED events"

"$BUILD/mas_coordinator" "$WORK" "$RES" "$HB" "$@" 2>"$T/coord.log" &
COORD=$!
PIDS+=("$COORD")
"$BUILD/mas_worker" "$WORK" "$RES" "$HB" "$T/w1.duckdb" w1 2>"$T/w1.log" &
W1=$!
PIDS+=("$W1")
"$BUILD/mas_worker" "$WORK" "$RES" "$HB" "$T/w2.duckdb" w2 2>"$T/w2.log" &
W2=$!
PIDS+=("$W2")

sleep 2   # mid-first-file for real day-files (2-7 s each)
kill -9 "$W1"
echo "killed w1 (pid $W1) at t+2s"

# Coordinator must finish on its own: death detection (30 s) + re-dispatch
# + remaining work. Watchdog well above worst case.
WATCHDOG=300
for _ in $(seq "$WATCHDOG"); do
    kill -0 "$COORD" 2>/dev/null || break
    sleep 1
done
if kill -0 "$COORD" 2>/dev/null; then
    echo "--- coordinator log (watchdog expired) ---"; cat "$T/coord.log"
    echo "FAIL: coordinator still running after ${WATCHDOG}s"; exit 1
fi
COORD_EXIT=0; wait "$COORD" || COORD_EXIT=$?
wait "$W2" 2>/dev/null || true

echo "--- coordinator log ---"; cat "$T/coord.log"
echo "--- w1 log ---"; cat "$T/w1.log"
echo "--- w2 log ---"; cat "$T/w2.log"
[ "$COORD_EXIT" -eq 0 ] || { echo "FAIL: coordinator exit $COORD_EXIT"; exit 1; }
grep -q "dead (silent" "$T/coord.log" || { echo "FAIL: no death detected"; exit 1; }
grep -q "re-dispatch" "$T/coord.log" || { echo "FAIL: no re-dispatch"; exit 1; }

# Merge every store, the written-off one included: intact -> harmless
# idempotent duplicates; corrupt -> mas_merge skips it loudly (Task 5).
"$BUILD/mas_merge" "$T/merged.duckdb" MCC777eda3db57348ef8a3113a642ae74db \
    "$T/w1.duckdb" "$T/w2.duckdb" 2>"$T/merge.log" || { cat "$T/merge.log"; exit 1; }
cat "$T/merge.log"
ROWS="$(sed -n 's/.*dst holds \([0-9]*\) rows.*/\1/p' "$T/merge.log")"

if [ "$ROWS" = "$EXPECTED" ]; then
    echo "PASS: merged $ROWS == oracle $EXPECTED (one worker killed mid-run)"
else
    echo "FAIL: merged $ROWS != oracle $EXPECTED"; exit 1
fi
