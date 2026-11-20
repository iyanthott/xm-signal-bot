<#
  Bot 6 - Candle Breakout: the plain 55-day breakout (same rule as Bot 4), gated on the trigger bar
  being a strong-bodied candle, traded on GOLD. Demo trading only, its own magic number (570036),
  Telegram label [CBO]. Runs alongside the other bots.
    cd D:\Claude\XM\trading-signal-bot ; .\scripts\run_cbo_bot.ps1
  Pause trading instantly: create data\cbo\TRADING_PAUSED
#>
param([string]$Python = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")
Set-Location (Split-Path $PSScriptRoot -Parent)
New-Item -ItemType Directory -Force data\cbo | Out-Null
$crashes = New-Object System.Collections.Generic.List[datetime]
while ($true) {
  Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] starting Candle Breakout bot"
  & $Python -m poetry run python -m trading_signal_bot.cbo_bot --env .env --lock-file data\cbo\bot.lock
  if ($LASTEXITCODE -eq 0) { break }
  $now = Get-Date; $crashes.Add($now); $crashes.RemoveAll({ param($t) ($now - $t).TotalMinutes -gt 10 }) | Out-Null
  if ($crashes.Count -gt 5) { Write-Host "more than 5 crashes in 10 minutes - stopping"; exit 1 }
  Start-Sleep -Seconds 30
}
