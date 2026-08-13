# Keeps the ForexBot backend running 24/7.
# Restarts automatically if it ever crashes. The bot auto-starts on boot
# (AUTO_START_BOT=true), so trading resumes after any restart.
#
# Run manually:   powershell -ExecutionPolicy Bypass -File run_backend.ps1
# Or register it to start at login (see scripts/README.md).

$ErrorActionPreference = "Continue"
$backend = Join-Path $PSScriptRoot "..\backend"
$python  = Join-Path $backend "venv\Scripts\python.exe"
$logFile = Join-Path $PSScriptRoot "backend.log"

Set-Location $backend

while ($true) {
    "$(Get-Date -Format o)  starting backend..." | Tee-Object -FilePath $logFile -Append
    & $python -m uvicorn main:app --host 0.0.0.0 --port 8000 *>> $logFile
    "$(Get-Date -Format o)  backend exited (code $LASTEXITCODE), restarting in 3s..." | Tee-Object -FilePath $logFile -Append
    Start-Sleep -Seconds 3
}
