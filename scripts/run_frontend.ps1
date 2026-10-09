# Builds the dashboard once, then serves it and restarts it if it crashes.
$ErrorActionPreference = "Continue"
$frontend = Join-Path $PSScriptRoot "..\frontend"
$logFile  = Join-Path $PSScriptRoot "frontend.log"
$env:BACKEND_INTERNAL_URL = "http://127.0.0.1:8000"   # /api proxy target, baked in at build time

Set-Location $frontend

"$(Get-Date -Format o)  building frontend..." | Tee-Object -FilePath $logFile -Append
npm run build *>> $logFile

while ($true) {
    "$(Get-Date -Format o)  starting frontend..." | Tee-Object -FilePath $logFile -Append
    npm run start *>> $logFile
    "$(Get-Date -Format o)  frontend exited, restarting in 3s..." | Tee-Object -FilePath $logFile -Append
    Start-Sleep -Seconds 3
}
