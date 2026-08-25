#!/usr/bin/env bash
# Benchmark sweep (spec §5): mono T∈{1,2,4,8} + MAS N∈{1,2,4,8,16} across
# volumes {1,7,28} day-files, R=3 repeats, per-run correctness vs oracle.
# usage: bench/run_bench.sh [--quick]     (--quick = 1-day volume only)
set -euo pipefail

QUICK=0
ONLY=both              # both | mono | mas
VOLUMES_ARG=""         # e.g. "1 7"; empty = the default ladder
OUT_CSV_ARG=""
FORCE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --quick)   QUICK=1; shift ;;
        --only)    ONLY="$2"; shift 2 ;;
        --volumes) VOLUMES_ARG="$2"; shift 2 ;;
        --out)     OUT_CSV_ARG="$2"; shift 2 ;;
        --force)   FORCE=1; shift ;;
        *) echo "usage: $0 [--quick] [--only mono|mas|both] [--volumes \"1 7\"] [--out file.csv] [--force]" >&2
           exit 2 ;;
    esac
done
case "$ONLY" in (mono|mas|both) ;; (*) echo "--only must be mono, mas or both"; exit 2;; esac
BUILD="${BUILD_DIR:-build}"
ZIP="telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02.zip"
DATA="${DATA_DIR:-telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02}"
MACHINE="MCC777eda3db57348ef8a3113a642ae74db"
OUT_CSV="${OUT_CSV_ARG:-bench/results.csv}"
ROWS_PER_DAY=86399
REPEATS=3
WORK=tcp://127.0.0.1:5591 RES=tcp://127.0.0.1:5592 HB=tcp://127.0.0.1:5593

for exe in mas_monolith mas_merge mas_worker mas_coordinator; do
    [ -x "$BUILD/$exe" ] || [ -x "$BUILD/$exe.exe" ] \
        || { echo "missing $BUILD/$exe (build first)"; exit 2; }
done
[ -f python/oracle_union.py ] || { echo "missing python/oracle_union.py"; exit 2; }

# /usr/bin/time -l is BSD-only. On Windows the build provides win_time
# (bench/win_time.cpp), which prints the same two stderr lines parse_time()
# reads. Linux's GNU time (-v) is still unadapted — this only covers macOS
# and Windows; refusing beats parsing empty fields into the CSV.
if [ -x /usr/bin/time ]; then
    TIMEIT=(/usr/bin/time -l)
elif [ -x "$BUILD/win_time" ] || [ -x "$BUILD/win_time.exe" ]; then
    TIMEIT=("$BUILD/win_time")
else
    echo "no /usr/bin/time and no $BUILD/win_time — unsupported platform"; exit 2
fi

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
if [ -f "$ZIP" ]; then
    unzip -n -q "$ZIP" -d "$DATA" 2>/dev/null || unzip -n -q "$ZIP"   # zip layout may or may not nest
fi   # no zip with the CSVs already extracted is fine; the >=28 gate below still aborts otherwise
FILES=()
while IFS= read -r f; do FILES+=("$f"); done \
    < <(find "$DATA" -name '*.csv' | sort)
[ "${#FILES[@]}" -ge 28 ] || { echo "ABORT: ${#FILES[@]} CSVs found, need 28"; exit 2; }

VOLUMES=(1 7 28)
[ "$QUICK" = 1 ] && VOLUMES=(1)
[ -n "$VOLUMES_ARG" ] && VOLUMES=($VOLUMES_ARG)

T="$(mktemp -d /tmp/mas_bench.XXXXXX)"
PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do kill -9 "$p" 2>/dev/null || true; done; rm -rf "$T"; }
trap cleanup EXIT

# --- oracle cache: expected distinct (head_id, ts) rows per volume -----------
# python/oracle_union.py (independent Python reference, spec §11). The store
# identity is (machine_id, head_id, ts); the retired cap_seq key, and why it
# was false, are oracle_union.py's docstring to tell. On this contiguous,
# non-overlapping pool the union equals the per-file sum; the union form is
# kept so an overlapping or re-delivered file still yields the right answer.
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

# duckdb lives in the project venv, not necessarily in the system interpreter.
# Resolved once, loudly, rather than failing inside a run three volumes deep.
PY_DUCKDB="$(pwd)/.venv/bin/python"
[ -x "$PY_DUCKDB" ] || PY_DUCKDB=python3
if ! "$PY_DUCKDB" -c "import duckdb" 2>/dev/null; then
    echo "error: no python with duckdb ($PY_DUCKDB). Create the venv first:" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install -r python/requirements.txt" >&2
    exit 1
fi

mkdir -p bench
# Never truncate an existing results file by accident: a partial re-run
# (`--only mas --volumes "1 7"`) writes a fraction of the committed sweep's
# rows, and this `>` would replace it with them. Overwriting is explicit.
if [ -e "$OUT_CSV" ] && [ "$FORCE" != 1 ]; then
    echo "error: $OUT_CSV exists; pass --force to overwrite it," >&2
    echo "       or --out <file.csv> to write elsewhere and splice by hand" >&2
    exit 1
fi
# Provenance rides the artifact itself, not a prose file two directories
# away: results_cuda.csv already does this and the headline sweep did not.
# bench_plots.py reads with comment="#".
{
    echo "# generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "# host: $(uname -srm) / $(hostname)"
    echo "# hardware: $(sysctl -n machdep.cpu.brand_string 2>/dev/null || grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2- || echo unknown)"
    echo "# build_dir: $BUILD ($(grep -m1 '^CMAKE_BUILD_TYPE' "$BUILD/CMakeCache.txt" 2>/dev/null || echo 'CMAKE_BUILD_TYPE unknown'))"
    echo "# repeats: $REPEATS  volumes: ${VOLUMES[*]}"
} > "$OUT_CSV"
echo "arch,n_workers,threads,files,repeat,clean_s,merge_s,total_s,events,rows_per_s,events_per_s,peak_rss_mb,cpu_pct" >> "$OUT_CSV"

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
if [ "$ONLY" != mas ]; then
for v in "${VOLUMES[@]}"; do
    for th in 1 2 4 8; do
        for rep in 1 2 3; do
            R="$T/run" && rm -rf "$R" && mkdir "$R"
            "${TIMEIT[@]}" "$BUILD/mas_monolith" "$R/mono.duckdb" "$MACHINE" "$th" \
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

# --- parquet runs -------------------------------------------------------------
# Same clean path, different persistence: no index, no WAL, no merge. The
# comparison is against mono-1T's e2e. What the store share costs -- on which
# box, on which date -- is docs/bench/results.md's to state, not this file's.
#
# NOTE: the committed bench/results.csv carries NO parquet rows. The
# month-scale Parquet/DuckDB comparison was taken by the dedicated harness
# instead (bench/parquet-comparison/, whose numbers are the ones
# docs/bench/results.md quotes), and results.csv is kept as one coherent sweep
# rather than gaining three rows from a different day. Running this script
# rewrites results.csv from scratch, so it would add them.
#
# The block is exercised, not merely read: bench/parquet-comparison/
# run_bench_smoke.csv is a 1-day --only mono run of it, counts oracle-exact.
for v in "${VOLUMES[@]}"; do
    for rep in 1 2 3; do
        R="$T/run" && rm -rf "$R" && mkdir -p "$R/pq"
        # "${TIMEIT[@]}", not a hardcoded /usr/bin/time -l: the guard above
        # picked win_time on Windows for exactly this reason, and this block
        # was the one place that ignored it.
        "${TIMEIT[@]}" "$BUILD/mas_monolith" --format parquet "$R/pq" "$MACHINE" 1 \
            "${FILES[@]:0:$v}" 2>"$R/log" || { cat "$R/log"; exit 1; }
        line=$(grep '^monolith:' "$R/log") \
            || { echo "FAIL: no monolith summary line"; cat "$R/log"; exit 1; }
        ev=$(echo "$line"    | sed -n 's/.* files, \([0-9]*\) events.*/\1/p')
        clean=$(echo "$line" | sed -n 's/.*clean \([0-9.]*\) s.*/\1/p')
        total=$(echo "$line" | sed -n 's/.*total \([0-9.]*\) s.*/\1/p')
        # The path goes through the environment, not through the Python source.
        # Splicing it into the source meant an apostrophe broke the Python
        # literal one level up, before the SQL escaping it was supposedly
        # protected by could ever run -- the escape was decoration. Out-of-band,
        # both levels hold, and the quote-doubling is doing real work.
        rows=$(MAS_PQ_GLOB="$R/pq/*.parquet" "$PY_DUCKDB" -c '
import os, duckdb
g = os.environ["MAS_PQ_GLOB"].replace("'"'"'", "'"'"''"'"'")
print(duckdb.sql(f"SELECT COUNT(DISTINCT (machine_id, head_id, ts)) FROM read_parquet(\x27{g}\x27)").fetchone()[0])')
        check_count "$rows" "$v" "parquet v=$v rep=$rep"
        read -r real user sys rss < <(parse_time "$R/log")
        emit_row "parquet" 0 1 "$v" "$rep" "$clean" "0" "$total" "$ev" "$rss" "$user" "$sys" "$real"
        echo "done: parquet v=${v}d rep=$rep total=${total}s"
    done
done

fi   # end monolith block

# --- MAS runs ----------------------------------------------------------------
if [ "$ONLY" != mono ]; then
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
                # "$MACHINE" explicitly: worker_main defaults machine_id to
                # "MCC", and the 3-char default against the monolith's 35-char
                # id made the two architectures write different rows. That id is
                # the first column of the UNIQUE key, so the difference is not
                # free; docs/bench/results.md records what it cost and where.
                "${TIMEIT[@]}" "$BUILD/mas_worker" "$WORK" "$RES" "$HB" \
                    "$R/w$w.duckdb" "w$w" "$MACHINE" 2>"$R/w$w.log" &
                WPIDS+=($!); PIDS+=($!)
            done
            # --workers: gate dispatch on all N registering, else ZMQ PUSH
            # queues every file into the first connected pipe -- the slow-joiner
            # capture that made MAS flat across N and got the first sweep
            # discarded (docs/validation-log.md, 2026-07-11).
            "${TIMEIT[@]}" "$BUILD/mas_coordinator" "$WORK" "$RES" "$HB" \
                --workers "$n" \
                "${FILES[@]:0:$v}" 2>"$R/coord.log" || { cat "$R/coord.log"; exit 1; }
            for p in "${WPIDS[@]}"; do wait "$p" || true; done
            t_clean=$(python3 -c "import time; print(f'{time.time() - $t_start:.3f}')")

            srcs=(); for ((w = 1; w <= n; w++)); do srcs+=("$R/w$w.duckdb"); done
            t_m0=$(python3 -c 'import time; print(f"{time.time():.3f}")')
            "${TIMEIT[@]}" "$BUILD/mas_merge" "$R/merged.duckdb" "$MACHINE" \
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

fi   # end MAS block

echo "sweep complete: $(( $(wc -l < "$OUT_CSV") - 1 )) rows in $OUT_CSV"
