param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$BacktestArgs
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = "C:\Users\15369\Documents\Codex\600601_automation_env\Scripts\python.exe"

Push-Location $ProjectRoot
try {
    & $Python -m backtesting.cli @BacktestArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
