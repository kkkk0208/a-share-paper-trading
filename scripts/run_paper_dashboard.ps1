$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "_python.ps1")
$Python = Resolve-ProjectPython -Root $Root

Set-Location -LiteralPath (Join-Path $Root "paper_dashboard")
Write-Host "正在生成一次性随机访问令牌；请使用下方 PAPER_DASHBOARD 地址进入。"
& $Python server.py --host 127.0.0.1 --port 8765
exit $LASTEXITCODE
