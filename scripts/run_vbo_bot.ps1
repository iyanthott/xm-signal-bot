<#
  Bot 5 - Volatility Breakout: Larry Williams-style intraday breakout of today's open +/- 0.5x
  yesterday's range on ETH. Demo trading only, its own magic number (570035), Telegram label [VBO].
  Runs alongside the other bots.
    cd D:\Claude\XM\trading-signal-bot ; .\scripts\run_vbo_bot.ps1
  Pause trading instantly: create data\vbo\TRADING_PAUSED
#>
param([string]$Python = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")
Set-Location (Split-Path $PSScriptRoot -Parent)
New-Item -ItemType Directory -Force data\vbo | Out-Null
$crashes = New-Object System.Collections.Generic.List[datetime]
while ($true) {
  Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] starting Volatility Breakout bot"
  & $Python -m poetry run python -m trading_signal_bot.vbo_bot --env .env --lock-file data\vbo\bot.lock
  if ($LASTEXITCODE -eq 0) { break }
  $now = Get-Date; $crashes.Add($now); $crashes.RemoveAll({ param($t) ($now - $t).TotalMinutes -gt 10 }) | Out-Null
  if ($crashes.Count -gt 5) { Write-Host "more than 5 crashes in 10 minutes - stopping"; exit 1 }
  Start-Sleep -Seconds 30
}
