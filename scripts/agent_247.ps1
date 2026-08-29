# 24/7 watchdog for the CryptoAutoTradeBot trading AGENT (the backend).
#
# Keeps uvicorn alive forever: if the backend is down it (re)starts it; if it's already
# healthy (e.g. you started it manually) it just monitors. Port-aware, so it never
# double-starts and never fights an existing instance. No admin rights needed.
#
# Runs automatically at logon via the Startup-folder launcher (agent_247.cmd), and can
# also be run by hand:  powershell -ExecutionPolicy Bypass -File scripts\agent_247.ps1
$ErrorActionPreference = "SilentlyContinue"

$repo    = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $repo "backend"
$py      = Join-Path $backend "venv\Scripts\python.exe"
$logDir  = Join-Path $repo "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$wlog = Join-Path $logDir "agent_247.log"
$blog = Join-Path $logDir "backend.out.log"
$berr = Join-Path $logDir "backend.err.log"

function Log($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m" | Out-File -Append -Encoding utf8 $wlog }
function Backend-Up {
  # "Up" = something is LISTENING on :8000. Port-based, not HTTP: during a heavy tick an
  # HTTP probe can time out even though the server is fine, which would wrongly spawn a
  # duplicate uvicorn that then fails to bind. The port check avoids that entirely.
  [bool](Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)
}

Log "watchdog started (pid $PID)"
while ($true) {
  if (-not (Backend-Up)) {
    Log "backend not healthy -> starting uvicorn"
    Start-Process -FilePath $py -WorkingDirectory $backend -WindowStyle Hidden `
      -ArgumentList "-m","uvicorn","main:app","--host","0.0.0.0","--port","8000" `
      -RedirectStandardOutput $blog -RedirectStandardError $berr
    Start-Sleep -Seconds 20
  } else {
    Start-Sleep -Seconds 15
  }
}
