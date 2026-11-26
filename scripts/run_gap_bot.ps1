<#
  Bot 8 - Open Gap Fade: at the 16:30 server US cash open, trades AGAINST an overnight move >= 0.75 x daily ATR
  on NAS100; flat by 19:00 server. Demo trading only, its own magic number (570038),
  Telegram label [GAP]. Runs alongside the other bots.
    cd D:\Claude\XM\trading-signal-bot ; .\scripts\run_gap_bot.ps1
  Pause trading instantly: create data\gap\TRADING_PAUSED
#>
param([string]$Python = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")
Set-Location (Split-Path $PSScriptRoot -Parent)
New-Item -ItemType Directory -Force data\gap | Out-Null
$crashes = New-Object System.Collections.Generic.List[datetime]
while ($true) {
  Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] starting Open Gap Fade bot"
  & $Python -m poetry run python -m trading_signal_bot.gap_bot --env .env --lock-file data\gap\bot.lock
  if ($LASTEXITCODE -eq 0) { break }
  $now = Get-Date; $crashes.Add($now); $crashes.RemoveAll({ param($t) ($now - $t).TotalMinutes -gt 10 }) | Out-Null
  if ($crashes.Count -gt 5) { Write-Host "more than 5 crashes in 10 minutes - stopping"; exit 1 }
  Start-Sleep -Seconds 30
}
