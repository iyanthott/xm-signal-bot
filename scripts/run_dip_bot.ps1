<#
  Bot 3 - Daily Dip: buys sharp daily dips in US indices (US500, NAS100) that are in a long-term uptrend.
  Demo trading only, its own magic number (570033), Telegram label [DIP]. Runs alongside the other two bots.
    cd D:\Claude\XM\trading-signal-bot ; .\scripts\run_dip_bot.ps1
  Pause trading instantly: create data\dip\TRADING_PAUSED
#>
param([string]$Python = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")
Set-Location (Split-Path $PSScriptRoot -Parent)
New-Item -ItemType Directory -Force data\dip | Out-Null
$crashes = New-Object System.Collections.Generic.List[datetime]
while ($true) {
  Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] starting Daily Dip bot"
  & $Python -m poetry run python -m trading_signal_bot.dip_bot --env .env --lock-file data\dip\bot.lock
  if ($LASTEXITCODE -eq 0) { break }
  $now = Get-Date; $crashes.Add($now); $crashes.RemoveAll({ param($t) ($now - $t).TotalMinutes -gt 10 }) | Out-Null
  if ($crashes.Count -gt 5) { Write-Host "more than 5 crashes in 10 minutes - stopping"; exit 1 }
  Start-Sleep -Seconds 30
}
