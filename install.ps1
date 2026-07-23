# omp-agent installer for Windows — bootstraps omp itself (if it isn't
# already on this machine), fetches the bridge, and hands off to the
# interactive setup wizard.
#
#   irm https://punakawan.raprast.asia/install.ps1 | iex
#
# Note: the bridge's background service today is systemd --user (Linux
# only). On Windows, `setup` still writes ~/.omp-agent/.env and this script
# still gets omp + the bridge source in place, but you run the bridge
# yourself (`python bridge.py`) — there's no native Windows service wired up
# yet.

$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/raka-raprast/punakawan.git"
$SrcDir = if ($env:OMP_AGENT_SRC) { $env:OMP_AGENT_SRC } else { Join-Path $env:USERPROFILE ".omp-agent\src" }
$OmpInstallUrl = "https://omp.sh/install.ps1"

function Write-Banner {
    Write-Host ""
    Write-Host "+-----------------------------------------------------------+" -ForegroundColor Cyan
    Write-Host "|                  omp-agent installer                      |" -ForegroundColor Cyan
    Write-Host "|         omp <-> Telegram bridge, one command away         |" -ForegroundColor Cyan
    Write-Host "+-----------------------------------------------------------+" -ForegroundColor Cyan
    Write-Host ""
}

function Write-InfoLine    { param($Message) Write-Host "-> $Message" -ForegroundColor Cyan }
function Write-SuccessLine { param($Message) Write-Host "OK $Message" -ForegroundColor Green }
function Write-WarnLine    { param($Message) Write-Host "!  $Message" -ForegroundColor Yellow }
function Write-ErrLine     { param($Message) Write-Host "X  $Message" -ForegroundColor Red }

Write-Banner

# ── 1. omp itself ─────────────────────────────────────────────────────────────
# The bridge is a thin frontend over the omp binary; without it there's
# nothing to run a conversation through, so it comes first.

$ompCmd = Get-Command omp -ErrorAction SilentlyContinue
if ($ompCmd) {
    Write-SuccessLine "omp found: $($ompCmd.Source)"
} else {
    Write-InfoLine "omp not found - installing it (irm $OmpInstallUrl | iex)"
    try {
        Invoke-RestMethod $OmpInstallUrl | Invoke-Expression
    } catch {
        Write-ErrLine "omp install failed: $_"
        Write-ErrLine "Install it manually from https://omp.sh, then re-run this script."
        exit 1
    }
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "User") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $ompCmd = Get-Command omp -ErrorAction SilentlyContinue
    if ($ompCmd) {
        Write-SuccessLine "omp installed: $($ompCmd.Source)"
    } else {
        Write-WarnLine "omp installed but not on PATH in this session - restart your terminal, or the setup wizard will let you point at a custom path."
    }
}

# ── 2. Prerequisites ──────────────────────────────────────────────────────────

$pythonCmd = $null
foreach ($candidate in @("python3", "python", "py")) {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($found) { $pythonCmd = $found.Source; break }
}
if (-not $pythonCmd) {
    Write-ErrLine "Python 3 is required. Install it from https://python.org, then re-run this script."
    exit 1
}
Write-SuccessLine "python found: $pythonCmd"

$gitCmd = Get-Command git -ErrorAction SilentlyContinue

# ── 3. omp-agent source ───────────────────────────────────────────────────────

$scriptDir = $null
if ($PSScriptRoot) { $scriptDir = $PSScriptRoot }

if ($scriptDir -and (Test-Path (Join-Path $scriptDir "bridge.py"))) {
    # Running from a local checkout — use it as-is.
    $SrcDir = $scriptDir
    Write-SuccessLine "using local checkout: $SrcDir"
} elseif (Test-Path (Join-Path $SrcDir ".git")) {
    Write-InfoLine "updating existing checkout at $SrcDir"
    Push-Location $SrcDir
    try {
        git pull --ff-only
        if ($LASTEXITCODE -ne 0) { throw "git pull failed with exit code $LASTEXITCODE" }
    } finally {
        Pop-Location
    }
    Write-SuccessLine "updated: $SrcDir"
} else {
    if (-not $gitCmd) {
        Write-ErrLine "git is required to fetch omp-agent. Install Git for Windows: https://git-scm.com/download/win"
        exit 1
    }
    Write-InfoLine "cloning omp-agent into $SrcDir"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SrcDir) | Out-Null
    git clone --depth 1 $RepoUrl $SrcDir
    if ($LASTEXITCODE -ne 0) { throw "git clone failed with exit code $LASTEXITCODE" }
    Write-SuccessLine "cloned: $SrcDir"
}

Write-Host ""
Write-InfoLine "handing off to the setup wizard..."
Write-Host ""
& $pythonCmd (Join-Path $SrcDir "bridge.py") setup
exit $LASTEXITCODE
