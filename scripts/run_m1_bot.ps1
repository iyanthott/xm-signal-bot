<#
  Second bot instance: M1-only signals, demo auto-trading with its own magic number, risk, state files and Telegram label [M1].
  Runs alongside the main bot (scripts\run_supervised.ps1). Website settings are OFF for this instance (--no-remote).
    cd D:\Claude\XM\trading-signal-bot ; .\scripts\run_m1_bot.ps1
  Pause trading instantly: create data\m1\TRADING_PAUSED
#>
param([string]$Python = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")
Set-Location (Split-Path $PSScriptRoot -Parent)
New-Item -ItemType Directory -Force data\m1 | Out-Null
$crashes = New-Object System.Collections.Generic.List[datetime]
while ($true) {
  Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] starting M1 bot"
  & $Python -m poetry run trading-signal-bot --config config\settings.m1_bot.yaml --env .env.m1 --no-remote --lock-file data\m1\bot.lock
  if ($LASTEXITCODE -eq 0) { break }
  $now = Get-Date; $crashes.Add($now); $crashes.RemoveAll({ param($t) ($now - $t).TotalMinutes -gt 10 }) | Out-Null
  if ($crashes.Count -gt 5) { Write-Host "more than 5 crashes in 10 minutes - stopping"; exit 1 }
  Start-Sleep -Seconds 30
}
