param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$BacktestArgs
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "_python.ps1")
$Python = Resolve-ProjectPython -Root $ProjectRoot

Push-Location $ProjectRoot
try {
    & $Python -m backtesting.cli @BacktestArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
