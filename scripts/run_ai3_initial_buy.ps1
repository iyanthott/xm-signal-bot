<#
  ONE-TIME Set 3 "3rd AI" initial basket buy. Retries internally every ~12 min until all
  8 positions fill (see scripts/ai3_initial_buy.py), then exits 0 - this wrapper stops
  retrying once the script itself exits cleanly, unlike the perpetual bot supervisors.

    cd D:\Claude\XM\trading-signal-bot ; .\scripts\run_ai3_initial_buy.ps1
#>
param([string]$Python = "C:\Users\Adrian\AppData\Local\pypoetry\Cache\virtualenvs\trading-signal-bot-MPMutSCp-py3.12\Scripts\python.exe")
Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] starting 3rd AI initial basket buy (one-time, exits when done)"
& $Python scripts\ai3_initial_buy.py
$code = $LASTEXITCODE
Write-Host "[$(Get-Date -Format 'HH:mm:ss')] ai3_initial_buy.py exited with code $code"
exit $code
