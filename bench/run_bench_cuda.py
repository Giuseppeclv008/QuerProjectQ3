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
import glob
import os
import platform
import re
import shutil
import subprocess
import sys
import time
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

# The build directory is resolved once, ROOT-anchored, and shared by the binary
# lookup and the provenance header. They must agree: a header naming a build
# type the measured binaries did not come from is exactly the defect the
# provenance row exists to close, and it fails silently. README documents
# build-bench for the MAS_BENCH_ONLY configure; build/ is the in-tree default
# and stays supported. BUILD_DIR overrides both, as run_bench.sh already allows.
_BUILD_OVERRIDE = os.environ.get("BUILD_DIR")
BUILD_CANDIDATES = (_BUILD_OVERRIDE,) if _BUILD_OVERRIDE else ("build-bench", "build")
_resolved_build = None

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


def resolve_build_dir():
    """The ROOT-relative build directory that actually holds the bench binaries.

    Decided by bench_cpu, cached, and reused by provenance() so the CSV header
    describes the build that was measured. Only the candidate directories are
    searched: a stale side build (build-plan once held an unoptimized bench_cpu,
    CMAKE_BUILD_TYPE empty) must never be silently benchmarked in place of the
    real one.

    Ambiguity is refused rather than ranked. Preference by position is what
    produced the bug this function replaced -- "always build/" simply became
    "always build-bench/", moving the silent-wrong-binary failure to a different
    developer. This repo accumulates build directories (.gitignore lists six),
    so a fresh build/ from the top-level README's instructions sitting beside a
    months-old build-bench/ is an ordinary state, and either answer is a guess.
    Set BUILD_DIR to say which.
    """
    global _resolved_build
    if _resolved_build is None:
        exe = "bench_cpu" + (".exe" if os.name == "nt" else "")
        found = [d for d in BUILD_CANDIDATES
                 if any(os.path.isfile(os.path.join(ROOT, sub, exe))
                        for sub in (d, os.path.join(d, "Release")))]
        if len(found) > 1:
            die(f"{' and '.join(found)} both hold {exe}; which one to benchmark "
                "is a guess, and benchmarking the stale one is silent",
                f"BUILD_DIR={found[0]} python bench/run_bench_cuda.py ...")
        if found:
            _resolved_build = found[0]
    return _resolved_build


def find_binary(name):
    """MSVC multi-config puts binaries in build/Release; make both work.

    Every binary comes from the single directory resolve_build_dir() picked, so
    one run cannot mix binaries from two different configures.
    """
    d = resolve_build_dir()
    if d is None:
        return None
    exe = name + (".exe" if os.name == "nt" else "")
    for sub in (d, os.path.join(d, "Release")):
        p = os.path.join(ROOT, sub, exe)
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
    # The py-numpy row is attributed in the README to pandas' CSV parser and
    # float_precision="round_trip"; without the version that claim cannot be
    # reproduced from the artifact. run_bench.sh has recorded its build type
    # since it was written and this driver -- the one that produces the headline
    # CUDA CSV -- recorded neither, so a Debug configure would have yielded a
    # file indistinguishable from a Release one. Spec 6.5 asks for the compiler
    # too.
    for mod in ("numpy", "pandas"):
        try:
            lines.append(f"# {mod}: {__import__(mod).__version__}")
        except Exception:                                        # noqa: BLE001
            lines.append(f"# {mod}: not importable")
    # The cache read here must be the one find_binary() drew the binaries from,
    # and ROOT-anchored: a CWD-relative lookup silently yields three `unknown`
    # lines from any directory but the repo root, and a CSV that looks complete
    # while naming nothing is worse than one that names the gap out loud.
    build_dir = resolve_build_dir()
    cache = os.path.join(ROOT, build_dir, "CMakeCache.txt") if build_dir else ""
    wanted = ("CMAKE_BUILD_TYPE", "CMAKE_CXX_COMPILER_ID",
              "CMAKE_CXX_COMPILER_VERSION")
    found = {}
    if not cache or not os.path.exists(cache):
        print(f"WARNING: no CMakeCache.txt under {build_dir or 'any build dir'}; "
              "the build-type and compiler fields will read 'unknown'",
              file=sys.stderr)
    try:
        with open(cache, encoding="utf-8") as fh:
            for line in fh:
                key = line.split(":", 1)[0]
                if key in wanted:
                    found[key] = line.strip().split("=", 1)[-1]
    except OSError:
        pass
    # Only the build type is in the cache; the compiler ID and version live in
    # CMakeFiles/<cmake-version>/CMakeCXXCompiler.cmake, which is where spec 6.5's
    # two fields actually are.
    # ROOT-anchored like the cache itself: with no build dir resolved, cache is
    # "" and os.path.dirname("") is "", which would glob relative to the CWD --
    # the same class of bug the cache lookup above was fixed for.
    cc_root = os.path.dirname(cache) if cache else os.path.join(ROOT, "__none__")
    for cc in glob.glob(os.path.join(cc_root, "CMakeFiles",
                                     "*", "CMakeCXXCompiler.cmake")):
        try:
            text = open(cc, encoding="utf-8").read()
        except OSError:
            continue
        for key in ("CMAKE_CXX_COMPILER_ID", "CMAKE_CXX_COMPILER_VERSION"):
            m = re.search(rf'set\({key} "([^"]*)"\)', text)
            if m and key not in found:
                found[key] = m.group(1)
    for key in wanted:
        lines.append(f"# {key.lower()}: {found.get(key, 'unknown')}")
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
    if not m:
        # A missing metrics line used to default to (0.0, 0.0, 0.0), and
        # emit() then wrote total_s=0.000, rows_per_s="0", cpu_pct="0" --
        # exactly the bogus row this harness exists to refuse.
        die(f"{' '.join(str(c) for c in cmd)} printed no metrics line\n{blob}")
    wall, cpu, rss = float(m.group(2)), float(m.group(3)), float(m.group(4))
    ev = last_match(_EVENTS, blob)
    cl = last_match(_CLEAN, blob)
    mg = last_match(_MERGE, blob)
    events = int(ev.group(1)) if ev else 0
    clean_s = float(cl.group(1)) if cl else wall
    merge_s = float(mg.group(1)) if mg else 0.0
    return blob, wall, cpu, rss, events, clean_s, merge_s


def machine_id_of(path):
    """The machine id embedded in a pool day-file's name.

    telemetry_<id>_<date>.csv -- the same derivation run_bench.sh hardcodes.
    """
    base = os.path.basename(path)
    parts = base.split("_")
    if len(parts) < 3 or parts[0] != "telemetry":
        die(f"cannot derive machine id from {base}; expected telemetry_<id>_<date>.csv")
    return parts[1]


def oracle_union(files):
    out = subprocess.run(
        [sys.executable, "oracle_union.py"] + [os.path.relpath(f, PY_DIR) for f in files],
        cwd=PY_DIR, capture_output=True, text=True)
    if out.returncode != 0:
        die(f"oracle_union.py failed:\n{out.stdout}{out.stderr}")
    return int(out.stdout.strip())


def emit(writer, arch, mode, threads, nfiles, rep, clean_s, total_s, events,
         rss, cpu_s, merge_s=0.0, note=""):
    """total_s is the whole process for every row: cpp/cuda/mono report their
    own wall in the metrics line, and the Python contenders' is measured
    around the subprocess by py_arch_time. rows_per_s, events_per_s and
    cpu_pct all divide by it -- one denominator, one meaning, every row.
    rss/cpu_s may be None (the Python contenders do not measure them): an
    unmeasured quantity is an empty cell, never a 0.0 pretending to be one.
    """
    writer.writerow({
        "arch": arch, "mode": mode, "n_workers": 0, "threads": threads,
        "files": nfiles, "repeat": rep,
        "clean_s": f"{clean_s:.3f}", "merge_s": f"{merge_s:.3f}",
        "total_s": f"{total_s:.3f}", "events": events,
        "rows_per_s": f"{ROWS_PER_DAY * nfiles / total_s:.1f}" if total_s else "0",
        "events_per_s": f"{events / total_s:.1f}" if total_s else "0",
        "peak_rss_mb": "" if rss is None else f"{rss:.1f}",
        "cpu_pct": ("" if cpu_s is None
                    else f"{100.0 * cpu_s / total_s:.1f}" if total_s else "0"),
        "note": note,
    })


def py_arch_time(module, files):
    """Time a Python cleaner. Returns (events, clean_s, proc_wall_s).

    clean_s is the in-process extract loop; proc_wall_s is the whole
    subprocess (interpreter start and imports included), measured here so the
    python rows' total_s means the same thing as the cpp/cuda rows' -- one
    denominator, one meaning, every row.
    """
    script = (
        "import sys, time; sys.path.insert(0, '.');"
        f"import {module} as m;"
        "t=time.perf_counter(); n=0\n"
        "for p in sys.argv[1:]: n += len(m.extract(p))\n"
        "print(f'{n} events'); print(f'clean {time.perf_counter()-t:.3f} s')"
    )
    t0 = time.perf_counter()
    p = subprocess.run([sys.executable, "-c", script] + files,
                       cwd=PY_DIR, capture_output=True, text=True)
    proc_wall = time.perf_counter() - t0
    if p.returncode != 0:
        die(f"{module} failed:\n{p.stdout}{p.stderr}")
    blob = p.stdout + p.stderr
    return (int(last_match(_EVENTS, blob).group(1)),
            float(last_match(_CLEAN, blob).group(1)),
            proc_wall)


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
        die(f"bench_cpu not found under {' or '.join(BUILD_CANDIDATES)}",
            "cmake -S . -B build-bench -DMAS_BENCH_ONLY=ON -DMAS_ENABLE_CUDA=ON && "
            "cmake --build build-bench --config Release")
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
              "directory without it keeps it ON. Reconfigure THIS directory "
              "with -DMAS_BENCH_ONLY=OFF to include them -- a separate build "
              "directory is not searched, so it would guarantee the opposite. "
              "Use BUILD_DIR=<dir> to point elsewhere.\n")

    # oracle_union counts distinct (head_id, ts) -- the store identity. The
    # day-files are contiguous and non-overlapping, so this EQUALS the sum of
    # per-file event counts: the same number gates both the e2e rows (store
    # row count) and the store-free clean rows (emitted events). The old
    # rationale for not gating clean runs ("store-free runs emit more than
    # the store keeps") was the retired cap_seq-key theory;
    # python/oracle_union.py's docstring records why it was false.
    print("Row counts every run should land on (oracle_union):")
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
            if cuda:
                # The bitwise CPU differential, once per volume, OUTSIDE the
                # timed repeats: a mismatch dies with the dump before any
                # number is recorded, and the untimed pass doubles as the GPU
                # warm-up repeat 1 never had (clock ramp, pinned-buffer first
                # touch, context creation).
                print(f"verify: cuda v={v}d (untimed differential + warm-up)")
                run([cuda, "--verify"] + sub)
            for rep in range(1, REPEATS + 1):
                # Every arch cleaning the same files must emit the same
                # number of events (cross-arch agreement), and that number
                # must equal the independent Python oracle (absolute gate,
                # checked after the arch loop) -- agreement alone would pass
                # five implementations sharing one bug.
                seen = {}

                # --- Python contenders ---------------------------------------
                for arch, module in (("py-naive", "oracle"),
                                     ("py-numpy", "clean_vectorized")):
                    if arch == "py-naive" and v > PY_NAIVE_MAX_FILES:
                        continue
                    rel = [os.path.relpath(f, PY_DIR) for f in sub]
                    events, clean_s, proc_wall = py_arch_time(module, rel)
                    seen[arch] = events
                    # total_s = subprocess wall (same denominator as cpp/cuda);
                    # rss/cpu are None -- unmeasured is an empty cell, not 0.0.
                    emit(w, arch, "clean", 1, v, rep, clean_s, proc_wall,
                         events, None, None)
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
                    blob, wall, cpu_s, rss, events, clean_s, _m = run(
                        [cuda] + sub)
                    seen["cuda"] = events
                    # total_s is the process wall clock, not the sum of stage
                    # timers: the stage sum used to hide the event
                    # materialization and every cudaMalloc/cudaHostAlloc, and
                    # at 28 day-files the hidden part cost about as much as the
                    # reported one. The differential --verify run happens once
                    # per volume BEFORE the repeats (see below): it used to run
                    # inside repeat 1's timed wall, making that row's total_s
                    # 58.9 s against 8.4 s and its derived rates meaningless.
                    emit(w, "cuda", "clean", 1, v, rep, clean_s, wall, events,
                         rss, cpu_s)
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
                if seen and distinct != {oracle[v]}:
                    die(f"arches agree with each other but not with the "
                        f"oracle at {v} day-file(s), repeat {rep}: "
                        f"{distinct.pop()} vs oracle {oracle[v]}",
                        "five implementations sharing one bug is still wrong; "
                        "investigate before re-running the sweep")

                # --- e2e, only if the full build is present -------------------
                if mono:
                    for arch, th, flag in (("mono-1T", 1, ["--no-store"]),
                                           ("mono-1T", 1, []),
                                           ("mono-MT", 8, [])):
                        mode = "clean" if flag else "e2e"
                        out_db = os.path.join(ROOT, "bench_tmp.duckdb")
                        # The pool's real 35-char id, not "MCC": the 3-char id
                        # stays inline in DuckDB and roughly halves per-row
                        # write cost (run_bench.sh and docs/bench/results.md
                        # document this at length), so "MCC" made the e2e rows
                        # unrepresentative of the store share.
                        _, wall, cpu_s, rss, events, clean_s, merge_s = run(
                            [mono] + flag + [out_db, machine_id_of(sub[0]), str(th)] + sub)
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
