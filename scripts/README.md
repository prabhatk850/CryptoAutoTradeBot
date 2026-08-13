# Keeping ForexBot running 24/7

The **backend** is what trades — it must stay up for the bot to work. The bot
auto-starts on boot (`AUTO_START_BOT=true` in `.env`), so trading resumes
automatically after any restart or crash.

## Option A — Quick (keep a window open)
Double-click or run:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_backend.ps1
```
It restarts the backend automatically if it crashes. Logs → `scripts\backend.log`.
Closing the window stops it. (Run `run_frontend.ps1` too if you want the dashboard up.)

## Option B — Start automatically at login (recommended)
Register a Task Scheduler task so it launches on every login and restarts on failure:

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PWD\scripts\run_backend.ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
Register-ScheduledTask -TaskName "ForexBotBackend" -Action $action -Trigger $trigger -Settings $settings -Description "ForexBot trading backend"
```
Manage it later in **Task Scheduler** (search it in Start). Delete with:
```powershell
Unregister-ScheduledTask -TaskName "ForexBotBackend" -Confirm:$false
```

## Option C — True 24/7 (runs even when your PC is off)
Your PC must be on for Options A/B. For real always-on, deploy the backend to a
cheap cloud host (Railway, Render, or a $5 VPS). MongoDB (Atlas) and Delta are
already cloud, so only the backend needs to move. Ask and I'll prepare the deploy.

## Notes
- **PC sleep stops it.** Set Windows power plan to "never sleep" if you want it
  running overnight on the desktop.
- Toggle auto-start with `AUTO_START_BOT=false` in `.env` if you'd rather start
  the bot manually from the dashboard.
