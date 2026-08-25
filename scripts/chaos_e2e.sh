#!/usr/bin/env bash
# Chaos E2E (resilience spec §10), both directions:
#   1. kill -9 a WORKER mid-run: the run completes and merged counts match
#      the oracle.
#   2. kill -9 the COORDINATOR mid-run: every worker exits on its own within
#      the idle budget -- no orphans.
# usage: scripts/chaos_e2e.sh <day1.csv> <day2.csv> [day3.csv ...]
set -euo pipefail

[ $# -ge 2 ] || { echo "usage: $0 <day1.csv> <day2.csv> [more.csv ...]"; exit 2; }
BUILD="${BUILD_DIR:-build}"
for exe in mas_coordinator mas_worker mas_merge; do
    [ -x "$BUILD/$exe" ] || { echo "missing $BUILD/$exe (build first)"; exit 2; }
done

# The machine id the day-files carry (telemetry_<id>_<date>.csv). Workers used
# to be launched with no machine_id at all -- the old argv default "MCC" --
# and the merge destination below is labelled with the real id, producing a
# store no machine-scoped analytics query would find. It passed only because
# count() is unscoped; machine_id is a required argument now.
MACHINE="$(basename "$1")"; MACHINE="${MACHINE#telemetry_}"; MACHINE="${MACHINE%%_*}"
[ -n "$MACHINE" ] || { echo "cannot derive machine id from $1"; exit 2; }

T="$(mktemp -d /tmp/mas_chaos.XXXXXX)"
PIDS=()
cleanup() {
    for p in "${PIDS[@]:-}"; do kill -9 "$p" 2>/dev/null || true; done
    rm -rf "$T"
}
trap cleanup EXIT

WORK=tcp://127.0.0.1:5571 RES=tcp://127.0.0.1:5572 HB=tcp://127.0.0.1:5573

# Oracle: python/oracle_union.py, the same reference bench/run_bench.sh uses.
#
# This used to sum the per-file counts from the `clean` CLI and compare the sum
# to the merged store's row count — the exact comparison run_bench.sh documents
# as wrong, and the reason oracle_union.py exists. It agreed only because the
# three day-files this script is given (02-01..03) all sit before the counter
# reset. Run it over a range that spans one and the chaos test would fail for a
# reason with nothing to do with resilience.
#
# oracle_union.py counts distinct (head_id, ts) — what the store holds — and is
# independent of every C++ binary under test.
EXPECTED="$(python3 python/oracle_union.py "$@")" \
    || { echo "oracle_union.py failed"; exit 1; }
case "$EXPECTED" in (*[!0-9]*|"") echo "oracle count failed: '$EXPECTED'"; exit 1;; esac
echo "oracle total: $EXPECTED events"

# --workers 2: the registration gate gets its only end-to-end exercise here
# (unit tests fake the transport). Both workers say hello before dispatch, so
# PUSH round-robins over both pipes instead of queueing everything into the
# first -- and a regression in the gate now fails this script instead of
# passing invisibly.
"$BUILD/mas_coordinator" "$WORK" "$RES" "$HB" --workers 2 "$@" 2>"$T/coord.log" &
COORD=$!
PIDS+=("$COORD")
"$BUILD/mas_worker" "$WORK" "$RES" "$HB" "$T/w1.duckdb" w1 "$MACHINE" 2>"$T/w1.log" &
W1=$!
PIDS+=("$W1")
"$BUILD/mas_worker" "$WORK" "$RES" "$HB" "$T/w2.duckdb" w2 "$MACHINE" 2>"$T/w2.log" &
W2=$!
PIDS+=("$W2")

# Kill 1 s after both workers have REGISTERED, not a fixed 2 s after launch:
# the fixed sleep was a real-time race -- on a fast machine the first file
# could already be done, the killed worker held nothing, and the re-dispatch
# assertion failed for a reason with nothing to do with resilience. One
# second past registration is mid-first-file for real day-files (2-7 s each).
for _ in $(seq 50); do
    grep -q "worker w1 joined" "$T/coord.log" 2>/dev/null \
        && grep -q "worker w2 joined" "$T/coord.log" 2>/dev/null && break
    sleep 0.2
done
sleep 1
kill -9 "$W1"
echo "killed w1 (pid $W1) ~1s after registration"

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
# idempotent duplicates; corrupt -> mas_merge skips it loudly.
"$BUILD/mas_merge" "$T/merged.duckdb" "$MACHINE" \
    "$T/w1.duckdb" "$T/w2.duckdb" 2>"$T/merge.log" || { cat "$T/merge.log"; exit 1; }
cat "$T/merge.log"
ROWS="$(sed -n 's/.*dst holds \([0-9]*\) rows.*/\1/p' "$T/merge.log")"

if [ "$ROWS" = "$EXPECTED" ]; then
    echo "PASS direction 1: merged $ROWS == oracle $EXPECTED (one worker killed mid-run)"
else
    echo "FAIL: merged $ROWS != oracle $EXPECTED"; exit 1
fi

# ---- Direction 2 (spec criterion 2): kill the COORDINATOR -----------------
# Workers must notice on their own -- 60 empty 1 s ticks then a voluntary
# idle exit -- and leave no orphan. Budget: 60 s idle + current file + margin.
echo
echo "=== direction 2: coordinator killed mid-run ==="
"$BUILD/mas_coordinator" "$WORK" "$RES" "$HB" --workers 2 "$@" 2>"$T/coord2.log" &
COORD2=$!
PIDS+=("$COORD2")
"$BUILD/mas_worker" "$WORK" "$RES" "$HB" "$T/v1.duckdb" v1 "$MACHINE" 2>"$T/v1.log" &
V1=$!
PIDS+=("$V1")
"$BUILD/mas_worker" "$WORK" "$RES" "$HB" "$T/v2.duckdb" v2 "$MACHINE" 2>"$T/v2.log" &
V2=$!
PIDS+=("$V2")

for _ in $(seq 50); do
    grep -q "worker v1 joined" "$T/coord2.log" 2>/dev/null \
        && grep -q "worker v2 joined" "$T/coord2.log" 2>/dev/null && break
    sleep 0.2
done
sleep 1
kill -9 "$COORD2"
echo "killed coordinator (pid $COORD2) ~1s after registration"

ORPHAN_BUDGET=90
for _ in $(seq "$ORPHAN_BUDGET"); do
    if ! kill -0 "$V1" 2>/dev/null && ! kill -0 "$V2" 2>/dev/null; then
        break
    fi
    sleep 1
done
FAILED2=0
for p in "$V1" "$V2"; do
    if kill -0 "$p" 2>/dev/null; then
        echo "FAIL: worker pid $p still alive ${ORPHAN_BUDGET}s after the coordinator died"
        FAILED2=1
    fi
done
[ "$FAILED2" -eq 0 ] || exit 1
echo "PASS direction 2: both workers exited on their own (no orphans)"
echo
echo "PASS: both chaos directions"
