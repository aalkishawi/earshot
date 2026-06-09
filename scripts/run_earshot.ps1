# Earshot scheduled-run wrapper.
#
# Called by Windows Task Scheduler. Runs `earshot run` (full pipeline) and
# appends all stdout + stderr to data/logs/earshot-YYYY-MM-DD.log.
#
# Exits with the same exit code as `earshot run` so Task Scheduler reports
# failures in its History tab.
#
# Test manually:
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\run_earshot.ps1

$ErrorActionPreference = 'Continue'

$root    = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$logDir  = Join-Path $root 'data\logs'
$exe     = Join-Path $root '.venv\Scripts\earshot.exe'

if (-not (Test-Path $exe)) {
    Write-Error "earshot.exe not found at $exe -- venv missing?"
    exit 127
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$today   = Get-Date -Format 'yyyy-MM-dd'
$logFile = Join-Path $logDir "earshot-$today.log"

$startStamp = (Get-Date).ToString('o')
Add-Content -Path $logFile -Value ""
Add-Content -Path $logFile -Value "=== started $startStamp ==="

# cmd.exe handles native-command redirection without PowerShell 5.1's
# stderr-wrap pitfall. Both streams get appended; exit code is propagated.
& cmd.exe /c "`"$exe`" run >> `"$logFile`" 2>&1"
$rc = $LASTEXITCODE

$endStamp = (Get-Date).ToString('o')
Add-Content -Path $logFile -Value "=== finished $endStamp exit=$rc ==="
exit $rc
