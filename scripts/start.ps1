# start.ps1 — Start the E-Labs backend on Windows
# Usage: .\scripts\start.ps1 [-Prod]
param([switch]$Prod)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
$Backend = Join-Path $Root "backend"

# Load .env if present
$EnvFile = Join-Path $Root ".env"
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
            [System.Environment]::SetEnvironmentVariable($Matches[1].Trim(), $Matches[2].Trim(), "Process")
        }
    }
    Write-Host ".env loaded"
}

# Activate venv if present
$Venv = Join-Path $Root ".venv\Scripts\Activate.ps1"
if (Test-Path $Venv) {
    & $Venv
} else {
    Write-Warning "No .venv found. Run: python -m venv .venv ; .venv\Scripts\Activate.ps1 ; pip install -r backend\requirements.txt"
}

$Host_ = if ($env:BACKEND_HOST) { $env:BACKEND_HOST } else { "127.0.0.1" }
$Port  = if ($env:BACKEND_PORT) { $env:BACKEND_PORT } else { "8001" }

Set-Location $Backend

if ($Prod) {
    Write-Host "Starting uvicorn PRODUCTION on $Host_:$Port"
    uvicorn app:app --host $Host_ --port $Port --workers 2
} else {
    Write-Host "Starting uvicorn DEV (--reload) on $Host_:$Port"
    uvicorn app:app --host $Host_ --port $Port --reload
}
