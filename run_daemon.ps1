$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$utf8 = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

function Test-Python([string]$Candidate) {
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    & $Candidate -c "import sys; print(sys.executable)" *> $null
    return $LASTEXITCODE -eq 0
}

$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (Test-Python $venvPython) {
    $python = $venvPython
} else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw 'Usable Python was not found. Run setup_windows.bat to rebuild .venv.'
    }
    $python = $pythonCommand.Source
}

Write-Host "Starting finance bot with: $python"
& $python 'local_finance_bot.py' daemon
exit $LASTEXITCODE
