# Keeps the ForexBot dashboard (Next.js) running. Builds once, then serves.
# Restarts automatically if it crashes.
$ErrorActionPreference = "Continue"
$frontend = Join-Path $PSScriptRoot "..\frontend"
$logFile  = Join-Path $PSScriptRoot "frontend.log"

Set-Location $frontend

# Production build once (skip if .next already built and up to date)
"$(Get-Date -Format o)  building frontend..." | Tee-Object -FilePath $logFile -Append
npm run build *>> $logFile

while ($true) {
    "$(Get-Date -Format o)  starting frontend..." | Tee-Object -FilePath $logFile -Append
    npm run start *>> $logFile
    "$(Get-Date -Format o)  frontend exited, restarting in 3s..." | Tee-Object -FilePath $logFile -Append
    Start-Sleep -Seconds 3
}
