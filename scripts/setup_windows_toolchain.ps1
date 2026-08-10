# Installs the two build prerequisites the CUDA benchmark needs on Windows and
# that cannot be installed without elevation: the VS 2022 C++ workload (MSVC +
# Windows SDK) and the NVIDIA CUDA Toolkit. CMake is NOT installed here -- the
# repo venv already carries it (`pip install cmake ninja`), so after this script
# the venv alone can configure and build.
#
# Run from an elevated PowerShell, or let the UAC prompts through:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_windows_toolchain.ps1
#
# Both installers are official (Microsoft VS Installer, NVIDIA via winget) and
# total roughly 6 GB of downloads. Reboot is not required; a NEW shell is, so
# vcvars/nvcc land on PATH.

$ErrorActionPreference = "Stop"

$vsInstaller = "C:\Program Files (x86)\Microsoft Visual Studio\Installer\setup.exe"
$vsPath = "C:\Program Files\Microsoft Visual Studio\2022\Community"

# --- 1. VS 2022 C++ workload (MSVC toolset + Windows SDK) --------------------
$haveMsvc = Test-Path "$vsPath\VC\Tools\MSVC"
if ($haveMsvc) {
    Write-Host "MSVC toolset already present, skipping the VS step."
} else {
    if (-not (Test-Path $vsInstaller)) {
        throw "VS Installer not found at $vsInstaller -- install VS 2022 first."
    }
    Write-Host "Adding the C++ workload to the existing VS 2022 Community (10-20 min)..."
    # setup.exe is a bootstrapper: without --wait it hands off to an elevated
    # child and returns at once, so the toolset check below would fire while the
    # install had barely started. Belt and braces: --wait plus Start-Process -Wait.
    $p = Start-Process -FilePath $vsInstaller -Wait -PassThru -ArgumentList @(
        "modify", "--installPath", $vsPath,
        "--add", "Microsoft.VisualStudio.Workload.NativeDesktop",
        "--includeRecommended", "--passive", "--norestart", "--wait")
    # 0 = ok, 3010 = ok + reboot pending, 862968 = ok + reboot pending (new installer)
    if ($p.ExitCode -notin 0, 3010, 862968) {
        throw ("VS setup exited $($p.ExitCode). Log: %TEMP%\dd_setup_*.log -- " +
               "or add the 'Desktop development with C++' workload from the VS Installer GUI.")
    }
    if (-not (Test-Path "$vsPath\VC\Tools\MSVC")) {
        throw "VS setup exited 0 but no MSVC toolset appeared -- open the VS Installer GUI and check."
    }
}

# --- 2. CUDA Toolkit ---------------------------------------------------------
# After MSVC on purpose: the CUDA installer drops its VS integration into the
# toolsets it finds at install time.
if (Get-Command nvcc -ErrorAction SilentlyContinue) {
    Write-Host "nvcc already on PATH, skipping the CUDA step."
} else {
    Write-Host "Installing the CUDA Toolkit via winget (~3.5 GB)..."
    winget install --id Nvidia.CUDA -e --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "winget failed ($LASTEXITCODE). Manual fallback: https://developer.nvidia.com/cuda-downloads"
    }
}

Write-Host ""
Write-Host "Done. Open a NEW shell (so nvcc/cl are found) and run, from the repo root:"
Write-Host '  .venv\Scripts\cmake.exe -S . -B build -DMAS_BENCH_ONLY=ON -DMAS_ENABLE_CUDA=ON'
Write-Host '  .venv\Scripts\cmake.exe --build build --config Release'
Write-Host '  .venv\Scripts\python.exe bench\run_bench_cuda.py --data telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02'
