# Task Scheduler entry point. Use -File so Windows does not re-parse command text.
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Capture', 'Report')]
    [string]$Mode,
    [Parameter(Mandatory = $true)]
    [string]$PythonPath,
    [string]$ExpectedDate,
    [ValidateSet('opening_call', 'continuous', 'closing_call')]
    [string]$ExpectedPhase
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Root 'runtime\paper_trading'
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$LogName = if ($Mode -eq 'Capture') { "acceptance-$ExpectedPhase.log" } else { 'acceptance-report.log' }
$Log = Join-Path $LogDir $LogName
"$(Get-Date -Format o) Starting $Mode $ExpectedPhase" | Out-File -FilePath $Log -Append -Encoding utf8
if (-not (Test-Path -LiteralPath $PythonPath)) { throw "Python executable is missing: $PythonPath" }

if ($Mode -eq 'Capture') {
    if (-not $ExpectedDate -or -not $ExpectedPhase) { throw 'Capture requires ExpectedDate and ExpectedPhase.' }
    $env:NO_PROXY = 'qt.gtimg.cn,hq.sinajs.cn'
    $Arguments = @('-m', 'paper_trading.collector', 'capture', '--symbols', '002195.SZ', '002815.SZ', '000300.SH',
        '--label', $ExpectedPhase, '--expected-date', $ExpectedDate, '--expected-phase', $ExpectedPhase)
} else {
    $Day = Get-Date -Format 'yyyy-MM-dd'
    $Out = Join-Path (Join-Path $LogDir 'reports') "$Day.json"
    $Arguments = @('-m', 'paper_trading.collector', 'report', '--date', $Day, '--out', $Out)
}

Push-Location $Root
try {
    # Native stderr is logged even on the expected nonzero rejection/quality result.
    $ErrorActionPreference = 'Continue'
    & $PythonPath @Arguments 2>&1 | Out-File -FilePath $Log -Append -Encoding utf8
    $Result = $LASTEXITCODE
    "$(Get-Date -Format o) Finished $Mode $ExpectedPhase exit=$Result" | Out-File -FilePath $Log -Append -Encoding utf8
    exit $Result
} finally {
    Pop-Location
}
