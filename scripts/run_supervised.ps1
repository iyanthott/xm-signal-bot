<#
  Runs the bot and restarts it automatically when it asks to (exit code 75 = "new website settings are ready").
  Use this instead of running the bot directly when XM_SETTINGS_TOKEN is set in .env.

    cd D:\Claude\XM\trading-signal-bot
    .\scripts\run_supervised.ps1

  Exit codes from the bot:
    75  -> new settings validated; restart immediately (normal, expected)
    0   -> clean stop (Ctrl+C / shutdown); the supervisor stops too
    any other -> crash; wait, then retry. More than 5 crashes in 10 minutes -> stop and say so.
#>
param(
  [string]$Python = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
  [string]$Config = "config\settings.yaml"
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path $Python)) {
  Write-Error "Python not found at $Python. Pass -Python <path to python.exe> (3.10-3.12 with Poetry)."
  exit 1
}

$crashTimes = New-Object System.Collections.Generic.List[datetime]

while ($true) {
  Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] starting bot ($Config)"
  & $Python -m poetry run trading-signal-bot --config $Config
  $code = $LASTEXITCODE

  if ($code -eq 75) {
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] new settings ready - restarting"
    Start-Sleep -Seconds 2
    continue
  }
  if ($code -eq 0) {
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] bot stopped cleanly"
    break
  }

  $now = Get-Date
  $crashTimes.Add($now)
  $recent = @($crashTimes | Where-Object { ($now - $_).TotalMinutes -le 10 })
  Write-Warning "bot exited with code $code ($($recent.Count) crash(es) in the last 10 minutes)"
  if ($recent.Count -gt 5) {
    Write-Error "Too many crashes. Stopping. Check logs\bot.log, fix the problem, then run this script again."
    exit $code
  }
  Start-Sleep -Seconds 30
}
