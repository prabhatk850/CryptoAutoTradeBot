# Runs the backend outside Docker and restarts it if it crashes (see scripts/README.md).
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
