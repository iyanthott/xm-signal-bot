<#
  Power of 2 (Set 2) EXPERIMENTAL forward-test bot: runs 3 archetypes from research/power_of_2_lab.py
  live (bb_breakout/NAS100, session_overlap_momentum/GOLD, donchian_scalp/NAS100) to validate the
  backtest on real market data. NOT a production bot - two of three only marginally passed train+test,
  one didn't pass train at all. Demo trading only, own magic numbers (570040-570042), own risk breaker
  (XM_P2_RISK_CAP_PCT), alerts to Adrian's personal Telegram (XM_P2_CHAT_ID). Runs alongside the other bots.
    cd D:\Claude\XM\trading-signal-bot ; .\scripts\run_p2_forwardtest_bot.ps1
  Pause trading instantly: create data\p2_forward_test\TRADING_PAUSED
#>
param([string]$Python = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")
Set-Location (Split-Path $PSScriptRoot -Parent)
New-Item -ItemType Directory -Force data\p2_forward_test | Out-Null
$crashes = New-Object System.Collections.Generic.List[datetime]
while ($true) {
  Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] starting Power of 2 forward-test bot"
  & $Python -m poetry run python -m trading_signal_bot.p2_forwardtest_bot --env .env --lock-file data\p2_forward_test\bot.lock
  if ($LASTEXITCODE -eq 0) { break }
  $now = Get-Date; $crashes.Add($now); $crashes.RemoveAll({ param($t) ($now - $t).TotalMinutes -gt 10 }) | Out-Null
  if ($crashes.Count -gt 5) { Write-Host "more than 5 crashes in 10 minutes - stopping"; exit 1 }
  Start-Sleep -Seconds 30
}
