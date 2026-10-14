# Real-time quote collector. Examples:
#   .\scripts\quote_collector.ps1 sync-calendar
#   .\scripts\quote_collector.ps1 run
#   .\scripts\quote_collector.ps1 report --date 2026-10-15
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CollectorArgs
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "_python.ps1")
$Python = Resolve-ProjectPython -Root $Root
if (-not $CollectorArgs) { $CollectorArgs = @("run") }

Push-Location $Root
try {
    & $Python -m paper_trading.collector @CollectorArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
