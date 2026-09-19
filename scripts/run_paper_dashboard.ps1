$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = "C:\Users\15369\Documents\Codex\600601_automation_env\Scripts\python.exe"

Set-Location -LiteralPath (Join-Path $Root "paper_dashboard")
Write-Host "模拟交易看板：http://127.0.0.1:8765"
& $Python server.py --host 127.0.0.1 --port 8765
exit $LASTEXITCODE
