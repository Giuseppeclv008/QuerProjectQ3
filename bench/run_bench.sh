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

for exe in mas_monolith mas_merge mas_worker mas_coordinator; do
    [ -x "$BUILD/$exe" ] || { echo "missing $BUILD/$exe (build first)"; exit 2; }
done
[ -f python/oracle_union.py ] || { echo "missing python/oracle_union.py"; exit 2; }

# --- disk guard --------------------------------------------------------------
# 2 GiB covers first-time extraction (~1.5 GiB) plus the per-run working set;
# once all 28 day CSVs are already extracted (idempotent unzip -n), only the
# working set remains, so require a 512 MB floor instead.
free_kb=$(df -k . | awk 'NR==2 {print $4}')
# `|| csv_count=0`: find exits 1 when $DATA doesn't exist yet; under pipefail
# that status reaches the assignment and set -e would kill the script silently
# before the guard can print ABORT (first-run path).
csv_count=$(find "$DATA" -name '*.csv' 2>/dev/null | wc -l | tr -d ' ') || csv_count=0
need_mb=2048
[ "$csv_count" -ge 28 ] && need_mb=512
[ "$free_kb" -ge $((need_mb * 1024)) ] || {
    echo "ABORT: only $((free_kb / 1024)) MB free; need >= $need_mb MB"; exit 2; }

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

# --- oracle cache: expected UNIQUE(head,cap_seq) rows per volume -------------
# python/oracle_union.py (independent Python reference, spec §11) instead of
# summing per-file `clean` counts: the real month's Count counter reset
# mid-day-16, so days 16-24 replay cap_seq ranges from days 1-15 and the
# store's UNIQUE constraint dedupes them — the per-file sum overcounts the
# union (21,872,663 events vs 14,372,237 rows across 28 days; found at the
# Task 5 gate, where mono T=1 and T=8 both hold exactly the union count).
# Plain indexed array, NOT `declare -A`: /usr/bin/env bash resolves to macOS's
# stock bash 3.2.57 here (verified — no Homebrew bash on PATH), which lacks
# associative arrays (`declare -A` errors under set -e and kills the script
# before the first oracle line). Keys are always numeric volumes (1/7/28), so
# a sparse indexed array is equivalent.
declare -a ORACLE
for v in "${VOLUMES[@]}"; do
    n="$(python3 python/oracle_union.py "${FILES[@]:0:$v}")" || n=""
    case "$n" in (*[!0-9]*|"") echo "oracle failed for $v days: '$n'"; exit 1;; esac
    ORACLE[$v]=$n
    echo "oracle[$v days] = $n rows"
done

mkdir -p bench
echo "arch,n_workers,threads,files,repeat,clean_s,merge_s,total_s,events,rows_per_s,events_per_s,peak_rss_mb,cpu_pct" > "$OUT_CSV"

# parse /usr/bin/time -l output file -> "real user sys rss_bytes"
parse_time() {   # $1 = time-output file
    awk '
        /real/ && /user/ && /sys/ { real=$1; user=$3; sys=$5 }
        /maximum resident set size/ { rss=$1 }
        END { printf "%s %s %s %s\n", real, user, sys, rss }
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
            line=$(grep '^monolith:' "$R/log") \
                || { echo "FAIL: no monolith summary line"; cat "$R/log"; exit 1; }
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
            # Reset per rep: the EXIT trap must only ever target this run's
            # workers — a sweep-long list would kill -9 long-reaped (possibly
            # reused) PIDs at exit.
            PIDS=()
            WPIDS=()
            for ((w = 1; w <= n; w++)); do
                /usr/bin/time -l "$BUILD/mas_worker" "$WORK" "$RES" "$HB" \
                    "$R/w$w.duckdb" "w$w" 2>"$R/w$w.log" &
                WPIDS+=($!); PIDS+=($!)
            done
            # --workers: gate dispatch on all N registering, else ZMQ PUSH
            # queues every file into the first connected pipe (slow-joiner
            # capture, found by sweep #1 — MAS was flat across N).
            /usr/bin/time -l "$BUILD/mas_coordinator" "$WORK" "$RES" "$HB" \
                --workers "$n" \
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
            # Benchmark integrity: a degraded start (gate timed out with fewer
            # than n registered) or a late joiner would mislabel this row's
            # n_workers — require exactly n pre-dispatch registrations.
            joined=$(grep -c ' joined' "$R/coord.log") || joined=0
            [ "$joined" = "$n" ] && ! grep -q 'proceeding with' "$R/coord.log" \
                || { echo "FAIL: $joined of $n workers joined (or degraded start)"; cat "$R/coord.log"; exit 1; }

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
