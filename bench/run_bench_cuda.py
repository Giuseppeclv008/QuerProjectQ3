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
ROWS_PER_DAY = 86399
REPEATS = 3
VOLUMES = (1, 7, 28)
PY_NAIVE_MAX_FILES = 1        # spec §6.3: 28 days is ~2 h for the naive path

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
    """MSVC multi-config puts binaries in build/Release; make both work."""
    exe = name + (".exe" if os.name == "nt" else "")
    for d in ("build", "build/Release", "build-plan", "build-plan/Release"):
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
    """Run and return (stdout+stderr, wall_s, cpu_s, peak_rss_mb, events, clean_s)."""
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    blob = p.stdout + p.stderr
    if p.returncode != 0:
        die(f"{' '.join(str(c) for c in cmd)} exited {p.returncode}\n{blob}")
    m = _METRICS.search(blob)
    wall, cpu, rss = (float(m.group(2)), float(m.group(3)), float(m.group(4))) \
        if m else (0.0, 0.0, 0.0)
    ev = last_match(_EVENTS, blob)
    cl = last_match(_CLEAN, blob)
    events = int(ev.group(1)) if ev else 0
    clean_s = float(cl.group(1)) if cl else wall
    return blob, wall, cpu, rss, events, clean_s


def oracle_union(files):
    out = subprocess.run(
        [sys.executable, "oracle_union.py"] + [os.path.relpath(f, PY_DIR) for f in files],
        cwd=PY_DIR, capture_output=True, text=True)
    if out.returncode != 0:
        die(f"oracle_union.py failed:\n{out.stdout}{out.stderr}")
    return int(out.stdout.strip())


def emit(writer, arch, mode, threads, nfiles, rep, clean_s, total_s, events,
         rss, cpu_s, note=""):
    writer.writerow({
        "arch": arch, "mode": mode, "n_workers": 0, "threads": threads,
        "files": nfiles, "repeat": rep,
        "clean_s": f"{clean_s:.3f}", "merge_s": "0.000",
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
    args = ap.parse_args()

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

    # Reference only, and only meaningful for `e2e`: this is UNIQUE(head,
    # cap_seq) after the counter reset dedupes replayed ranges, i.e. what a
    # *store* ends up holding. Store-free runs emit more than this on
    # multi-day volumes. The `clean` gate is the cross-arch check below.
    print("Store row counts the e2e runs should land on (oracle_union):")
    oracle = {v: oracle_union(files[:v]) for v in volumes}
    for v, n in oracle.items():
        print(f"  {v:2d} day-file(s): {n} rows")

    with open(RESULTS, "w", newline="") as fh:
        for line in provenance():
            fh.write(line + "\n")
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()

        stage_fh = open(STAGES, "w", newline="")
        sw = csv.DictWriter(stage_fh, fieldnames=STAGE_FIELDS)
        sw.writeheader()

        naive_1day = []          # clean_s samples, used for the extrapolation

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
                        continue          # spec §6.3; extrapolated below
                    rel = [os.path.relpath(f, PY_DIR) for f in sub]
                    events, clean_s = py_arch_time(module, rel)
                    seen[arch] = events
                    if arch == "py-naive" and v == 1:
                        naive_1day.append(clean_s)
                    emit(w, arch, "clean", 1, v, rep, clean_s, clean_s, events, 0.0, 0.0)
                    print(f"done: {arch} v={v}d rep={rep} clean={clean_s:.3f}s")

                # --- C++ contender -------------------------------------------
                for arch, th in (("cpp-1T", 1), ("cpp-MT", 8)):
                    _, wall, cpu_s, rss, events, clean_s = run(
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
                    blob, wall, cpu_s, rss, events, clean_s = run(
                        [cuda] + verify + sub)
                    seen["cuda"] = events
                    # total_s is the process wall clock, not the sum of stage
                    # timers: the stage sum used to hide the event
                    # materialization and every cudaMalloc/cudaHostAlloc, and
                    # at 28 day-files the hidden part cost about as much as the
                    # reported one.
                    emit(w, "cuda", "clean", 1, v, rep, clean_s, wall, events, rss, cpu_s)
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
                        _, wall, cpu_s, rss, events, clean_s = run(
                            [mono] + flag + [out_db, "MCC", str(th)] + sub)
                        emit(w, arch, mode, th, v, rep, clean_s, wall, events, rss, cpu_s)
                        for stale in [out_db] + [
                                f"{out_db}.t{t}.duckdb" for t in range(th)]:
                            if os.path.exists(stale):
                                os.remove(stale)
                        print(f"done: {arch} [{mode}] v={v}d rep={rep} clean={clean_s:.3f}s")

        # --- py-naive extrapolation (spec §6.3) ------------------------------
        # The interpreted loop is ~40 min per repeat at 28 day-files, so it is
        # measured at 1 day only. The transform is O(rows) with no cross-file
        # state, so scaling the median linearly is sound for `clean` mode. Rows
        # are labelled so nobody mistakes them for measurements.
        if naive_1day:
            naive_1day.sort()
            median_1d = naive_1day[len(naive_1day) // 2]
            for v in volumes:
                if v <= PY_NAIVE_MAX_FILES:
                    continue
                est = median_1d * v
                emit(w, "py-naive", "clean", 1, v, 0, est, est, 0, 0.0, 0.0,
                     note="extrapolated")
                print(f"note: py-naive v={v}d extrapolated to {est:.1f}s "
                      f"from the 1-day median")

        stage_fh.close()

    print(f"\nsweep complete\n  {RESULTS}\n  {STAGES}")
    print("\nPaste both files back. Summary:")
    with open(RESULTS) as fh:
        for line in fh:
            if not line.startswith("#"):
                print("  " + line.rstrip())


if __name__ == "__main__":
    main()
