# CUDA cleaning benchmark

Measures the same cap-event transform in Python, C++, and CUDA on one machine.
See `docs/superpowers/specs/2026-08-10-cuda-cleaning-bench-design.md`.

## Prerequisites

| Platform | Needs |
|---|---|
| Windows | Visual Studio 2022 Build Tools, CUDA Toolkit, CMake, Python 3.9+ — if MSVC or CUDA are missing, `scripts\setup_windows_toolchain.ps1` (elevated) installs them; CMake comes from the venv (`pip install cmake`) |
| Linux | gcc/clang with C++20, CUDA Toolkit, CMake, Python 3.9+ |
| macOS | Xcode CLT, CMake, Python 3.9+ (no CUDA — CPU and Python arches only) |

## Run

```
cmake -S . -B build -DMAS_BENCH_ONLY=ON -DMAS_ENABLE_CUDA=ON
cmake --build build --config Release
pip install -r bench/requirements-bench.txt
python bench/run_bench_cuda.py --data telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02.zip
```

`--config Release` is required on Windows (MSVC is multi-config) and harmless
elsewhere. Add `--quick` to run the 1-day volume only.

Outputs `bench/results_cuda.csv` and `bench/results_cuda_stages.csv`. Paste both
back.

## Adding the `e2e` rows

`MAS_BENCH_ONLY=ON` builds no DuckDB, so there is no store to write and no `e2e`
mode. For those rows, configure the full build as well — and pass
`-DMAS_BENCH_ONLY=OFF` explicitly: it is a cached option, so reconfiguring the
same build directory without mentioning it keeps the cached ON, silently builds
no `mas_monolith`, and the driver skips every `e2e` row. (The same cache holds
`MAS_ENABLE_ZMQ=OFF` after a bench-only configure; state it too if you want the
agent runtime back later.)

```
cmake -S . -B build -DMAS_BENCH_ONLY=OFF -DMAS_ENABLE_CUDA=ON -DMAS_ENABLE_ZMQ=OFF
cmake --build build --config Release
```

The driver picks up `mas_monolith` automatically and adds the `e2e` rows; if it
still cannot find it, it says so at startup.

## If it fails

Every failure names what broke and the command that fixes it. Paste the whole
output — a correctness mismatch prints the first ten differing events with all
nine fields, which is enough to fix the bug without access to the machine.
