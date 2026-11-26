<#
  Bot 7 - Fresh-Breakout Dip: buys a sharp short dip (RSI(2) < 10) right after a fresh 20-day high,
  above the 50-day average, on CHFJPY / GBPJPY / EURJPY. Demo trading only, its own magic number (570037),
  Telegram label [FBD]. Runs alongside the other bots.
    cd D:\Claude\XM\trading-signal-bot ; .\scripts\run_fbd_bot.ps1
  Pause trading instantly: create data\fbd\TRADING_PAUSED
#>
param([string]$Python = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")
Set-Location (Split-Path $PSScriptRoot -Parent)
New-Item -ItemType Directory -Force data\fbd | Out-Null
$crashes = New-Object System.Collections.Generic.List[datetime]
while ($true) {
  Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] starting Fresh-Breakout Dip bot"
  & $Python -m poetry run python -m trading_signal_bot.fbd_bot --env .env --lock-file data\fbd\bot.lock
  if ($LASTEXITCODE -eq 0) { break }
  $now = Get-Date; $crashes.Add($now); $crashes.RemoveAll({ param($t) ($now - $t).TotalMinutes -gt 10 }) | Out-Null
  if ($crashes.Count -gt 5) { Write-Host "more than 5 crashes in 10 minutes - stopping"; exit 1 }
  Start-Sleep -Seconds 30
}
