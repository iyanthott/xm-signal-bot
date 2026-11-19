<#
  Bot 4 - Crypto Trend: daily 55/20-day channel breakout on BTCUSD and ETHUSD (long and short).
  Demo trading only, its own magic number (570034), Telegram label [TREND]. Runs alongside the other bots.
  Crypto trades 7 days a week, so this needs the PC (and MT5) on through weekends to act on every signal.
    cd D:\Claude\XM\trading-signal-bot ; .\scripts\run_trend_bot.ps1
  Pause trading instantly: create data\trend\TRADING_PAUSED
#>
param([string]$Python = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")
Set-Location (Split-Path $PSScriptRoot -Parent)
New-Item -ItemType Directory -Force data\trend | Out-Null
$crashes = New-Object System.Collections.Generic.List[datetime]
while ($true) {
  Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] starting Crypto Trend bot"
  & $Python -m poetry run python -m trading_signal_bot.trend_bot --env .env --lock-file data\trend\bot.lock
  if ($LASTEXITCODE -eq 0) { break }
  $now = Get-Date; $crashes.Add($now); $crashes.RemoveAll({ param($t) ($now - $t).TotalMinutes -gt 10 }) | Out-Null
  if ($crashes.Count -gt 5) { Write-Host "more than 5 crashes in 10 minutes - stopping"; exit 1 }
  Start-Sleep -Seconds 30
}
