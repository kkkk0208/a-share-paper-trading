# Schedule the remaining real samples and a daily quality report.
# Run while signed in; these tasks use the current interactive user session.
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [datetime]$SampleDate,
    [string]$PythonPath
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot '_python.ps1')
if (-not $PythonPath) { $PythonPath = $env:A_SHARE_PYTHON }
if (-not $PythonPath) { $PythonPath = [Environment]::GetEnvironmentVariable('A_SHARE_PYTHON', 'User') }
$Python = if ($PythonPath) { $PythonPath } else { Resolve-ProjectPython -Root $Root }
if (-not (Test-Path -LiteralPath $Python)) { throw "Python executable is missing: $Python" }
& $Python -c 'import sys; assert sys.version_info >= (3, 11)'
if ($LASTEXITCODE -ne 0) { throw "Python failed its startup check: $Python" }
$Calendar = Import-Csv (Join-Path $Root 'data\trade_dates.csv')
$Day = $SampleDate.ToString('yyyy-MM-dd')
if ($Day -notin $Calendar.trade_date) { throw "$Day is not listed as a trading day." }
if ($SampleDate.Date -le (Get-Date).Date) { throw 'SampleDate must be a future trading date.' }

$LogDir = Join-Path $Root 'runtime\paper_trading'
$Runner = Join-Path $PSScriptRoot 'run_quote_acceptance.ps1'
$Principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

foreach ($Sample in @(
    @{ Label = 'opening_call'; Time = '09:20' },
    @{ Label = 'continuous'; Time = '10:00' },
    @{ Label = 'closing_call'; Time = '14:58' }
)) {
    $Name = "AShareQuoteSample_$($Sample.Label)"
    $Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`" -Mode Capture -PythonPath `"$Python`" -ExpectedDate $Day -ExpectedPhase $($Sample.Label)"
    $Action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $Arguments -WorkingDirectory $Root
    $At = [datetime]::ParseExact("$Day $($Sample.Time)", 'yyyy-MM-dd HH:mm', [Globalization.CultureInfo]::InvariantCulture)
    $Trigger = New-ScheduledTaskTrigger -Once -At $At
    if ($PSCmdlet.ShouldProcess($Name, "Register sample at $At")) {
        New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
        Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Description 'Capture real quote sample, no orders' -Force | Out-Null
    }
    [pscustomobject]@{ TaskName = $Name; At = $At; ExpectedPhase = $Sample.Label }
}

$Name = 'AShareQuoteQualityReport'
$Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`" -Mode Report -PythonPath `"$Python`""
$Action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $Arguments -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 3:10pm
$ReportSettings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
if ($PSCmdlet.ShouldProcess($Name, 'Register weekday quality report at 15:10')) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger -Settings $ReportSettings -Principal $Principal -Description 'Daily paper-trading quote quality report, no orders' -Force | Out-Null
}
[pscustomobject]@{ TaskName = $Name; At = 'Weekdays 15:10'; ExpectedPhase = 'after_close' }
