# Full quality gate on Windows: lint, format check, strict types, unit + CLI tests, and the Tk GUI end-to-end tests.
#   powershell -ExecutionPolicy Bypass -File scripts\check.ps1
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
$py = if (Test-Path '.venv\Scripts\python.exe') { '.venv\Scripts\python.exe' } else { 'python' }

function Step([string]$name, [scriptblock]$run) {
  Write-Host "== $name"
  & $run
  if ($LASTEXITCODE -ne 0) { Write-Error "$name failed"; exit 1 }
}

Step 'ruff check'  { & $py -m ruff check chatbridge tests scripts }
Step 'ruff format' { & $py -m ruff format --check chatbridge tests scripts }
Step 'mypy'        { & $py -m mypy }
Step 'pytest (unit, CLI, Tk GUI)' { & $py -m pytest -W ignore }
Write-Host 'ALL CHECKS PASSED'
