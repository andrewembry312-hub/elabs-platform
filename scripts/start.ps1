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
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            $val = $Matches[2] -replace '\s*#.*$', '' # strip inline comments
            [System.Environment]::SetEnvironmentVariable($Matches[1].Trim(), $val.Trim(), "Process")
        }
    }
    Write-Host ".env loaded"
}

# Activate venv — check local .venv first, fall back to workspace-root venv
$Venv = Join-Path $Root ".venv\Scripts\Activate.ps1"
$WorkspaceVenv = Join-Path (Split-Path $Root -Parent) ".venv\Scripts\Activate.ps1"
if (Test-Path $Venv) {
    & $Venv
} elseif (Test-Path $WorkspaceVenv) {
    & $WorkspaceVenv
    Write-Host "Using workspace-root venv"
} else {
    Write-Warning "No .venv found. Run: python -m venv .venv ; .venv\Scripts\Activate.ps1 ; pip install -r backend\requirements.txt"
}

$BindHost = if ($env:BACKEND_HOST) { $env:BACKEND_HOST } else { "127.0.0.1" }
$Port     = if ($env:BACKEND_PORT) { $env:BACKEND_PORT } else { "8001" }

Set-Location $Backend

if ($Prod) {
    Write-Host "Starting uvicorn PRODUCTION on ${BindHost}:${Port}"
    uvicorn app:app --host $BindHost --port $Port --workers 2
} else {
    Write-Host "Starting uvicorn DEV (--reload) on ${BindHost}:${Port}"
    uvicorn app:app --host $BindHost --port $Port --reload
}
