# Reproducible registration of the weekday quote collector; no order submission.
[CmdletBinding(SupportsShouldProcess = $true)]
param([string]$PythonPath)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot '_python.ps1')
if (-not $PythonPath) { $PythonPath = $env:A_SHARE_PYTHON }
if (-not $PythonPath) { $PythonPath = [Environment]::GetEnvironmentVariable('A_SHARE_PYTHON', 'User') }
$Python = if ($PythonPath) { $PythonPath } else { Resolve-ProjectPython -Root $Root }
if (-not (Test-Path -LiteralPath $Python)) { throw "Python executable is missing: $Python" }
& $Python -c 'import sys; assert sys.version_info >= (3, 11)'
if ($LASTEXITCODE -ne 0) { throw "Python failed its startup check: $Python" }

$Runner = Join-Path $PSScriptRoot 'run_quote_acceptance.ps1'
$Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`" -Mode Collect -PythonPath `"$Python`""
$Action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $Arguments -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At (Get-Date -Hour 9 -Minute 5 -Second 0)
$Settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 8)
$Principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
if ($PSCmdlet.ShouldProcess('AShareQuoteCollector', 'Register weekday collector at 09:05')) {
    New-Item -ItemType Directory -Path (Join-Path $Root 'runtime\paper_trading') -Force | Out-Null
    Register-ScheduledTask -TaskName 'AShareQuoteCollector' -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Description 'A-share quote collector, paper trading only' -Force | Out-Null
}
[pscustomobject]@{ TaskName = 'AShareQuoteCollector'; At = 'Weekdays 09:05'; Python = $Python }
