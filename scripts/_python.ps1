# Shared helper: decides which python.exe the scripts use.
# Order: $env:A_SHARE_PYTHON, then .venv inside the project, then python on PATH.
# Set it once (new terminals pick it up):  setx A_SHARE_PYTHON "C:\path\to\python.exe"
function Resolve-ProjectPython {
    param([Parameter(Mandatory = $true)][string]$Root)
    if ($env:A_SHARE_PYTHON) {
        if (Test-Path -LiteralPath $env:A_SHARE_PYTHON) { return $env:A_SHARE_PYTHON }
        throw "A_SHARE_PYTHON points to a missing file: $env:A_SHARE_PYTHON"
    }
    $Venv = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $Venv) { return $Venv }
    $Command = Get-Command python -ErrorAction SilentlyContinue
    if ($Command) { return $Command.Source }
    throw "Python not found. Set the A_SHARE_PYTHON environment variable to python.exe."
}
