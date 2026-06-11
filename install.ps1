# Earshot — one-shot installer for Windows (PowerShell).
#
# Usage (from any directory):
#   iwr -useb https://raw.githubusercontent.com/aalkishawi/earshot/main/install.ps1 | iex
#
# Or save this file locally and run:
#   powershell -ExecutionPolicy Bypass -File install.ps1
#
# Creates a `.venv` in the current directory, installs Earshot into it from
# the GitHub main branch, and prints next-step commands.

$ErrorActionPreference = 'Stop'
$RepoUrl = 'git+https://github.com/aalkishawi/earshot.git'
$VenvDir = '.venv'

function Fail($msg) {
    Write-Host ""
    Write-Host "  install failed: $msg" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "  Earshot installer" -ForegroundColor Cyan
Write-Host "  -----------------" -ForegroundColor Cyan
Write-Host ""

# 1. Python version check.
try {
    $pyVersion = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
} catch {
    Fail "python isn't on PATH. Install Python 3.11+ from https://python.org/downloads"
}
$parts = $pyVersion.Split('.')
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 11)) {
    Fail "Python $pyVersion found, but Earshot needs 3.11+. Install a newer one from https://python.org/downloads"
}
Write-Host "  [OK]  Python $pyVersion" -ForegroundColor Green

# 2. Venv guard.
if (Test-Path $VenvDir) {
    Fail "$VenvDir already exists in $(Get-Location). Move or delete it, then re-run."
}

# 3. Create venv.
Write-Host "  [..] Creating venv at $VenvDir"
& python -m venv $VenvDir
if (-not (Test-Path "$VenvDir\Scripts\python.exe")) {
    Fail "venv creation failed."
}
Write-Host "  [OK]  venv created" -ForegroundColor Green

# 4. Install Earshot from GitHub.
Write-Host "  [..] Installing Earshot from $RepoUrl (takes ~30s)..."
& "$VenvDir\Scripts\pip.exe" install --quiet --upgrade pip
& "$VenvDir\Scripts\pip.exe" install --quiet $RepoUrl
if ($LASTEXITCODE -ne 0) {
    Fail "pip install returned exit $LASTEXITCODE"
}
if (-not (Test-Path "$VenvDir\Scripts\earshot.exe")) {
    Fail "earshot.exe not found after install"
}
$installedVersion = & "$VenvDir\Scripts\earshot.exe" --version
Write-Host "  [OK]  $installedVersion" -ForegroundColor Green

# 5. Next steps.
Write-Host ""
Write-Host "  Installed. Next:" -ForegroundColor Cyan
Write-Host "    1. Activate the venv:"
Write-Host "         .\$VenvDir\Scripts\Activate.ps1"
Write-Host "    2. Walk the wizard (have your Anthropic API key + Yahoo app password handy):"
Write-Host "         earshot init"
Write-Host "    3. Verify everything is green:"
Write-Host "         earshot doctor"
Write-Host "    4. Try it (first run baselines existing content — no digest yet):"
Write-Host "         earshot run"
Write-Host ""
