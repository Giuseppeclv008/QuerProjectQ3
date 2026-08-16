#!/usr/bin/env python3
"""Three-way cleaning benchmark: Python, C++, CUDA, one machine, one command.

Replaces bench/run_bench.sh for this sweep. That script needs bash,
/usr/bin/time -l, unzip, find, sed and awk, and works around macOS bash 3.2 --
none of which exist on the Windows box this is meant to run on. Python is
already a hard requirement here, since it is two of the contenders.

Usage:
    python bench/run_bench_cuda.py --data <month.zip | extracted_dir>
    python bench/run_bench_cuda.py --data ... --quick     # 1-day volume only
"""
import argparse
import csv
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY_DIR = os.path.join(ROOT, "python")
# One header + 86,399 data rows is the pool's day-file shape (validated on
# every extracted month). rows_per_s is computed from this constant, not from
# the rows each run actually read -- runs on a different pool would need it
# re-derived.
ROWS_PER_DAY = 86399
REPEATS = 3
VOLUMES = (1, 7, 28)
# Measured, not assumed: py-naive cleans one day-file in ~2.7 s, so the full
# 28-file month is ~75 s per repeat -- cheap enough to measure at every volume.
# (An earlier estimate of "~2 h for the naive path" was off by two orders of
# magnitude and justified extrapolating the larger volumes; the extrapolation
# and its CSV rows are gone with it.)
PY_NAIVE_MAX_FILES = max(VOLUMES)

RESULTS = os.path.join(ROOT, "bench", "results_cuda.csv")
STAGES = os.path.join(ROOT, "bench", "results_cuda_stages.csv")

FIELDS = ["arch", "mode", "n_workers", "threads", "files", "repeat", "clean_s",
          "merge_s", "total_s", "events", "rows_per_s", "events_per_s",
          "peak_rss_mb", "cpu_pct", "note"]
STAGE_FIELDS = ["files", "repeat", "read_s", "h2d_s", "index_s", "parse_s",
                "delta_s", "compact_s", "d2h_s", "materialize_s", "store_s"]

_METRICS = re.compile(
    r"metrics: tag=(\S+) wall_s=([\d.]+) cpu_s=([\d.]+) peak_rss_mb=([\d.]+)")
_EVENTS = re.compile(r"(\d+) events")
_CLEAN = re.compile(r"clean ([\d.]+) s")
_MERGE = re.compile(r"merge ([\d.]+) s")
_STAGE = re.compile(r"stages: (.*)")


def last_match(pattern, blob):
    """Final occurrence, not the first: with --verify the CUDA binary prints a
    per-file "verify ok: ... (N events)" line before its summary, so the first
    "N events" in the blob is day 1's count, not the total. Every binary prints
    its summary last."""
    m = None
    for m in pattern.finditer(blob):
        pass
    return m


def die(msg, fix=None):
    print(f"\nERROR: {msg}", file=sys.stderr)
    if fix:
        print(f"  fix: {fix}", file=sys.stderr)
    sys.exit(1)


def find_binary(name):
    """MSVC multi-config puts binaries in build/Release; make both work.

    Only the documented build directory is searched: a stale side build
    (build-plan once held an unoptimized bench_cpu, CMAKE_BUILD_TYPE empty)
    must never be silently benchmarked in place of the real one.
    """
    exe = name + (".exe" if os.name == "nt" else "")
    for d in ("build", "build/Release"):
        p = os.path.join(ROOT, d, exe)
        if os.path.isfile(p):
            return p
    return None


def extract_pool(data):
    """Return a sorted list of day-file paths, extracting the zip if needed."""
    if os.path.isdir(data):
        out_dir = data
    else:
        if not os.path.isfile(data):
            die(f"{data} is neither a directory nor a file")
        out_dir = os.path.splitext(data)[0]
        free_mb = shutil.disk_usage(ROOT).free // (1024 * 1024)
        if free_mb < 2048 and not os.path.isdir(out_dir):
            die(f"only {free_mb} MB free; extraction needs about 2 GB",
                "free some disk and retry")
        os.makedirs(out_dir, exist_ok=True)
        with zipfile.ZipFile(data) as z:
            members = [m for m in z.namelist() if m.endswith(".csv")]
            for m in members:
                target = os.path.join(out_dir, os.path.basename(m))
                if not os.path.exists(target):
                    with z.open(m) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
    files = sorted(
        os.path.join(out_dir, f) for f in os.listdir(out_dir) if f.endswith(".csv"))
    if not files:
        die(f"no CSV files under {out_dir}")
    return files


def provenance():
    lines = [
        f"# platform: {platform.platform()}",
        f"# processor: {platform.processor() or 'unknown'}",
        f"# python: {sys.version.split()[0]}",
    ]
    try:
        gpu = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=30)
        lines.append(f"# gpu: {gpu.stdout.strip() or 'none'}")
    except (OSError, subprocess.SubprocessError):
        lines.append("# gpu: nvidia-smi not found")
    # PATH is not enough: a shell older than the CUDA install (or one that
    # trims PATH) misses nvcc while CUDA_PATH is set -- fall back to it.
    nvcc = shutil.which("nvcc") or os.path.join(
        os.environ.get("CUDA_PATH", ""), "bin",
        "nvcc" + (".exe" if os.name == "nt" else ""))
    try:
        nv = subprocess.run([nvcc, "--version"], capture_output=True,
                            text=True, timeout=30)
        tail = nv.stdout.strip().splitlines()[-1] if nv.stdout.strip() else "unknown"
        lines.append(f"# nvcc: {tail}")
    except (OSError, subprocess.SubprocessError):
        lines.append("# nvcc: not found")
    return lines


def run(cmd, cwd=None):
    """Run and return (stdout+stderr, wall_s, cpu_s, peak_rss_mb, events, clean_s, merge_s).

    merge_s is 0.0 for every binary except the monolith, which is the only one
    whose summary prints a merge phase.
    """
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    blob = p.stdout + p.stderr
    if p.returncode != 0:
        die(f"{' '.join(str(c) for c in cmd)} exited {p.returncode}\n{blob}")
    m = _METRICS.search(blob)
    wall, cpu, rss = (float(m.group(2)), float(m.group(3)), float(m.group(4))) \
        if m else (0.0, 0.0, 0.0)
    ev = last_match(_EVENTS, blob)
    cl = last_match(_CLEAN, blob)
    mg = last_match(_MERGE, blob)
    events = int(ev.group(1)) if ev else 0
    clean_s = float(cl.group(1)) if cl else wall
    merge_s = float(mg.group(1)) if mg else 0.0
    return blob, wall, cpu, rss, events, clean_s, merge_s


def oracle_union(files):
    out = subprocess.run(
        [sys.executable, "oracle_union.py"] + [os.path.relpath(f, PY_DIR) for f in files],
        cwd=PY_DIR, capture_output=True, text=True)
    if out.returncode != 0:
        die(f"oracle_union.py failed:\n{out.stdout}{out.stderr}")
    return int(out.stdout.strip())


def emit(writer, arch, mode, threads, nfiles, rep, clean_s, total_s, events,
         rss, cpu_s, merge_s=0.0, note=""):
    """total_s is the whole run: the process wall clock where one is measured
    (cpp, cuda, mono), the in-process loop time for the Python contenders,
    which have no separate wall. rows_per_s, events_per_s and cpu_pct all
    divide by it -- one denominator, one meaning, every row."""
    writer.writerow({
        "arch": arch, "mode": mode, "n_workers": 0, "threads": threads,
        "files": nfiles, "repeat": rep,
        "clean_s": f"{clean_s:.3f}", "merge_s": f"{merge_s:.3f}",
        "total_s": f"{total_s:.3f}", "events": events,
        "rows_per_s": f"{ROWS_PER_DAY * nfiles / total_s:.1f}" if total_s else "0",
        "events_per_s": f"{events / total_s:.1f}" if total_s else "0",
        "peak_rss_mb": f"{rss:.1f}",
        "cpu_pct": f"{100.0 * cpu_s / total_s:.1f}" if total_s else "0",
        "note": note,
    })


def py_arch_time(module, files):
    """Time a Python cleaner in-process-per-file, summing events."""
    script = (
        "import sys, time; sys.path.insert(0, '.');"
        f"import {module} as m;"
        "t=time.perf_counter(); n=0\n"
        "for p in sys.argv[1:]: n += len(m.extract(p))\n"
        "print(f'{n} events'); print(f'clean {time.perf_counter()-t:.3f} s')"
    )
    p = subprocess.run([sys.executable, "-c", script] + files,
                       cwd=PY_DIR, capture_output=True, text=True)
    if p.returncode != 0:
        die(f"{module} failed:\n{p.stdout}{p.stderr}")
    blob = p.stdout + p.stderr
    return (int(last_match(_EVENTS, blob).group(1)),
            float(last_match(_CLEAN, blob).group(1)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="month zip or extracted directory")
    ap.add_argument("--quick", action="store_true", help="1-day volume only")
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing results CSVs (they are refused otherwise)")
    args = ap.parse_args()

    # A --quick run writes the same two files as the full sweep; without this
    # guard the documented smoke-test truncated the committed full-sweep CSVs.
    if not args.force:
        clobbered = [p for p in (RESULTS, STAGES) if os.path.exists(p)]
        if clobbered:
            die("refusing to overwrite: " + ", ".join(clobbered),
                "pass --force to overwrite, or move the files aside first")

    files = extract_pool(args.data)
    volumes = (1,) if args.quick else tuple(v for v in VOLUMES if v <= len(files))

    bench_cpu = find_binary("bench_cpu")
    cuda = find_binary("mas_cuda_clean")
    mono = find_binary("mas_monolith")
    if not bench_cpu:
        die("bench_cpu not found",
            "cmake -S . -B build -DMAS_BENCH_ONLY=ON -DMAS_ENABLE_CUDA=ON && "
            "cmake --build build --config Release")
    try:
        import numpy, pandas   # noqa: F401
    except ImportError:
        die("numpy and pandas are required",
            "pip install -r bench/requirements-bench.txt")
    if not cuda:
        print("WARNING: mas_cuda_clean not found -- CUDA rows will be missing. "
              "Configure with -DMAS_ENABLE_CUDA=ON to include them.\n")
    if not mono:
        print("WARNING: mas_monolith not found -- the e2e rows will be missing. "
              "MAS_BENCH_ONLY is a cached option: reconfiguring the same build "
              "directory without it keeps it ON. Configure with "
              "-DMAS_BENCH_ONLY=OFF (or a separate build dir) to include "
              "them.\n")

    # Reference only, and only meaningful for `e2e`: this is UNIQUE(head,
    # cap_seq) after the counter reset dedupes replayed ranges, i.e. what a
    # *store* ends up holding. Store-free runs emit more than this on
    # multi-day volumes. The `clean` gate is the cross-arch check below.
    print("Store row counts the e2e runs should land on (oracle_union):")
    oracle = {v: oracle_union(files[:v]) for v in volumes}
    for v, n in oracle.items():
        print(f"  {v:2d} day-file(s): {n} rows")

    # Both files under one with: die() raises SystemExit, and an sys.exit
    # mid-sweep used to leave results_cuda_stages.csv open and unflushed.
    with open(RESULTS, "w", newline="") as fh, \
         open(STAGES, "w", newline="") as stage_fh:
        for line in provenance():
            fh.write(line + "\n")
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()

        sw = csv.DictWriter(stage_fh, fieldnames=STAGE_FIELDS)
        sw.writeheader()

        for v in volumes:
            sub = files[:v]
            for rep in range(1, REPEATS + 1):
                # Every arch cleaning the same files must emit the same number of
                # events. That cross-check is the correctness gate for `clean`
                # mode: oracle_union counts UNIQUE(head, cap_seq) rows, which is
                # what a *store* holds after the counter reset dedupes replays --
                # not what a store-free run emits. Comparing against it here
                # would fail for the wrong reason on multi-day volumes.
                seen = {}

                # --- Python contenders ---------------------------------------
                for arch, module in (("py-naive", "oracle"),
                                     ("py-numpy", "clean_vectorized")):
                    if arch == "py-naive" and v > PY_NAIVE_MAX_FILES:
                        continue
                    rel = [os.path.relpath(f, PY_DIR) for f in sub]
                    events, clean_s = py_arch_time(module, rel)
                    seen[arch] = events
                    emit(w, arch, "clean", 1, v, rep, clean_s, clean_s, events, 0.0, 0.0)
                    print(f"done: {arch} v={v}d rep={rep} clean={clean_s:.3f}s")

                # --- C++ contender -------------------------------------------
                for arch, th in (("cpp-1T", 1), ("cpp-MT", 8)):
                    _, wall, cpu_s, rss, events, clean_s, _m = run(
                        [bench_cpu, str(th)] + sub)
                    seen[arch] = events
                    emit(w, arch, "clean", th, v, rep, clean_s, wall, events, rss, cpu_s)
                    print(f"done: {arch} v={v}d rep={rep} clean={clean_s:.3f}s")

                # --- CUDA ----------------------------------------------------
                if cuda:
                    # --verify on the first repeat runs the bitwise differential
                    # against CapEventExtractorFlat inside the binary; a mismatch
                    # exits non-zero and run() aborts the sweep with the dump.
                    verify = ["--verify"] if rep == 1 else []
                    blob, wall, cpu_s, rss, events, clean_s, _m = run(
                        [cuda] + verify + sub)
                    seen["cuda"] = events
                    # total_s is the process wall clock, not the sum of stage
                    # timers: the stage sum used to hide the event
                    # materialization and every cudaMalloc/cudaHostAlloc, and
                    # at 28 day-files the hidden part cost about as much as the
                    # reported one.
                    # note="verify": repeat 1 runs the full CPU differential
                    # inside the timed wall clock, a 7x spread in one column
                    # of one configuration -- a row that must be identifiable
                    # as such in the CSV, not discovered by counting repeats.
                    emit(w, "cuda", "clean", 1, v, rep, clean_s, wall, events,
                         rss, cpu_s, note="verify" if verify else "")
                    m = _STAGE.search(blob)
                    if m:
                        kv = dict(p.split("=") for p in m.group(1).split())
                        sw.writerow({"files": v, "repeat": rep, "store_s": "0.000",
                                     **{k: kv.get(k, "0") for k in STAGE_FIELDS[2:-1]}})
                    print(f"done: cuda v={v}d rep={rep} clean={clean_s:.3f}s")

                distinct = set(seen.values())
                if len(distinct) > 1:
                    die("arches disagree on event count at "
                        f"{v} day-file(s), repeat {rep}: {seen}",
                        "a fast implementation that is wrong must not produce a "
                        "number; re-run the disagreeing arch with --verify")

                # --- e2e, only if the full build is present -------------------
                if mono:
                    for arch, th, flag in (("mono-1T", 1, ["--no-store"]),
                                           ("mono-1T", 1, []),
                                           ("mono-MT", 8, [])):
                        mode = "clean" if flag else "e2e"
                        out_db = os.path.join(ROOT, "bench_tmp.duckdb")
                        _, wall, cpu_s, rss, events, clean_s, merge_s = run(
                            [mono] + flag + [out_db, "MCC", str(th)] + sub)
                        # The oracle is computed for every volume and was only
                        # ever printed; run_bench.sh gates every run and this
                        # sweep claimed to but did not. Store-backed runs must
                        # land on the store row count; --no-store runs emit
                        # raw events (more than the store keeps on multi-day
                        # volumes) and are covered by the cross-arch `seen`
                        # check above instead.
                        if mode == "e2e" and events != oracle[v]:
                            die(f"{arch} [e2e] at {v} day-file(s), repeat "
                                f"{rep}: {events} events, oracle says "
                                f"{oracle[v]}",
                                "a fast implementation that is wrong must not "
                                "produce a number; investigate before "
                                "re-running the sweep")
                        emit(w, arch, mode, th, v, rep, clean_s, wall, events,
                             rss, cpu_s, merge_s=merge_s)
                        for stale in [out_db] + [
                                f"{out_db}.t{t}.duckdb" for t in range(th)]:
                            if os.path.exists(stale):
                                os.remove(stale)
                        print(f"done: {arch} [{mode}] v={v}d rep={rep} clean={clean_s:.3f}s")


    print(f"\nsweep complete\n  {RESULTS}\n  {STAGES}")
    print("\nPaste both files back. Summary:")
    with open(RESULTS) as fh:
        for line in fh:
            if not line.startswith("#"):
                print("  " + line.rstrip())


if __name__ == "__main__":
    main()
