param(
    [Parameter(Mandatory=$true)][string]$Command,
    [Parameter(ValueFromRemainingArguments=$true)][string[]]$CommandArgs
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "_python.ps1")
$Python = Resolve-ProjectPython -Root $Root
$Config = Join-Path $Root "config\paper_trading.json"
$Runtime = Join-Path $Root "runtime\paper_trading"
$Database = Join-Path $Runtime "paper_trading.sqlite3"

if (-not (Test-Path -LiteralPath $Config)) {
    throw "Missing config\paper_trading.json. Review and copy config\paper_trading.example.json first."
}

Push-Location $Root
try {
    & $Python -m paper_trading.cli --config $Config --db $Database --runtime-dir $Runtime $Command @CommandArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
