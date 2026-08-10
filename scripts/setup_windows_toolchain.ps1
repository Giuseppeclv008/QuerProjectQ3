# Installs the two build prerequisites the CUDA benchmark needs on Windows and
# that cannot be installed without elevation: the VS 2022 C++ workload (MSVC +
# Windows SDK) and the NVIDIA CUDA Toolkit. CMake is NOT installed here -- the
# repo venv already carries it (`pip install cmake ninja`), so after this script
# the venv alone can configure and build.
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup_windows_toolchain.ps1
#
# Both installers are official (Microsoft VS Installer, NVIDIA via winget) and
# total roughly 6 GB of downloads. Reboot is not required; a NEW shell is, so
# nvcc/cl land on PATH.
#
# Waiting on the VS Installer is deliberately done by POLLING, not exit codes:
# this box's installer (4.3.2029) rejects `--wait` with exit 87, and a
# non-elevated setup.exe respawns itself elevated and returns at once, so
# Start-Process -Wait cannot be trusted either. The script therefore relaunches
# itself elevated once (one UAC prompt covers both installs), starts the modify,
# and watches for the MSVC toolset directory to appear and the installer
# processes to drain.

$ErrorActionPreference = "Stop"

$vsInstaller = "C:\Program Files (x86)\Microsoft Visual Studio\Installer\setup.exe"
$vsPath = "C:\Program Files\Microsoft Visual Studio\2022\Community"
$toolset = "$vsPath\VC\Tools\MSVC"
$cudaRoot = "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA"

function Test-Elevated {
    [Security.Principal.WindowsPrincipal]::new(
        [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Elevated)) {
    Write-Host "Relaunching elevated -- approve the single UAC prompt."
    $p = Start-Process powershell -Verb RunAs -Wait -PassThru -ArgumentList @(
        "-ExecutionPolicy", "Bypass", "-File", $MyInvocation.MyCommand.Path)
    # The elevated child did the work in its own window; verify from here.
    Write-Host ""
    if (Test-Path $toolset) { Write-Host "MSVC toolset: OK" }
    else { Write-Host "MSVC toolset: MISSING -- see the elevated window / VS Installer GUI" }
    if (Test-Path $cudaRoot) { Write-Host "CUDA Toolkit: OK" }
    else { Write-Host "CUDA Toolkit: MISSING -- see the elevated window" }
    if ((Test-Path $toolset) -and (Test-Path $cudaRoot)) {
        Write-Host ""
        Write-Host "Done. Open a NEW shell (so nvcc/cl are found) and run, from the repo root:"
        Write-Host '  .venv\Scripts\cmake.exe -S . -B build -DMAS_BENCH_ONLY=ON -DMAS_ENABLE_CUDA=ON'
        Write-Host '  .venv\Scripts\cmake.exe --build build --config Release'
        Write-Host '  .venv\Scripts\python.exe bench\run_bench_cuda.py --data telemetry_MCC777eda3db57348ef8a3113a642ae74db_2026-02'
    }
    exit $p.ExitCode
}

# --- elevated from here on ---------------------------------------------------

# The elevated window closes with the script; hold it open long enough to read
# a failure (the parent window only knows "missing", not why).
trap {
    Write-Host ""
    Write-Host "FAILED: $_" -ForegroundColor Red
    Start-Sleep -Seconds 120
    exit 1
}

# --- 1. VS 2022 C++ workload (MSVC toolset + Windows SDK) --------------------
if (Test-Path $toolset) {
    Write-Host "MSVC toolset already present, skipping the VS step."
} else {
    if (-not (Test-Path $vsInstaller)) {
        throw "VS Installer not found at $vsInstaller -- install VS 2022 first."
    }
    Write-Host "Adding the C++ workload to the existing VS 2022 Community (10-20 min)..."
    Start-Process -FilePath $vsInstaller -ArgumentList @(
        "modify", "--installPath", $vsPath,
        "--add", "Microsoft.VisualStudio.Workload.NativeDesktop",
        "--includeRecommended", "--passive", "--norestart")
    $deadline = (Get-Date).AddMinutes(45)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 15
        $busy = Get-Process -Name "setup", "vs_installer*", "vs_installershell*",
                            "vs_installerservice*" -ErrorAction SilentlyContinue
        if (-not $busy) {
            if (Test-Path $toolset) { break }
            throw ("The VS Installer exited but no MSVC toolset appeared -- check " +
                   "%TEMP%\dd_installer_*.log or add 'Desktop development with C++' " +
                   "from the VS Installer GUI, then re-run this script.")
        }
        Write-Host "  ...installer still running ($(Get-Date -Format HH:mm:ss))"
    }
    if (-not (Test-Path $toolset)) {
        throw "Timed out (45 min) waiting for the C++ workload -- finish it in the VS Installer window, then re-run this script."
    }
    Write-Host "MSVC toolset installed."
}

# --- 2. CUDA Toolkit ---------------------------------------------------------
# After MSVC on purpose: the CUDA installer drops its VS integration into the
# toolsets it finds at install time.
if ((Get-Command nvcc -ErrorAction SilentlyContinue) -or (Test-Path $cudaRoot)) {
    Write-Host "CUDA Toolkit already present, skipping the CUDA step."
} else {
    Write-Host "Installing the CUDA Toolkit via winget (~3.5 GB)..."
    winget install --id Nvidia.CUDA -e --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "winget failed ($LASTEXITCODE). Manual fallback: https://developer.nvidia.com/cuda-downloads"
    }
}

Write-Host ""
Write-Host "Elevated steps complete."
