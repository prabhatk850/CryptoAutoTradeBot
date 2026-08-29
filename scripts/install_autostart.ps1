# One-time setup: make the trading AGENT run 24/7 automatically.
#
# Registers a Windows Scheduled Task that launches the backend watchdog
# (run_backend.ps1) at every logon and restarts it on failure. The watchdog keeps
# uvicorn alive; AUTO_START_BOT=true makes the agent loop resume on every boot.
# No admin rights needed (runs as the current user, limited privileges).
#
#   Install:  powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
#   Remove :  Unregister-ScheduledTask -TaskName 'CryptoAutoTradeBot' -Confirm:$false

$ErrorActionPreference = "Stop"
$repo  = Split-Path -Parent $PSScriptRoot
$watch = Join-Path $repo "scripts\run_backend.ps1"
$task  = "CryptoAutoTradeBot"

if (-not (Test-Path $watch)) { throw "watchdog not found: $watch" }

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$watch`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
# Restart the watchdog if it ever dies; never time it out; start even on battery / if a
# trigger was missed. This is what makes it genuinely 24/7 rather than "until it hiccups".
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings `
  -Description "CryptoAutoTradeBot trading agent - 24/7 backend watchdog" -Force | Out-Null

Write-Host "Registered scheduled task '$task' (runs at logon, restarts on failure)."
Write-Host "It will also keep running now via: Start-ScheduledTask -TaskName '$task'"
