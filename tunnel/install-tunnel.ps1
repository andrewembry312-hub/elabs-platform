# install-tunnel.ps1
# One-shot script to install and run the E-Labs Cloudflare Tunnel on Windows
# Run as Administrator in PowerShell
#
# Usage:
#   .\tunnel\install-tunnel.ps1
# ─────────────────────────────────────────────────────────────────────────────

param(
    [string]$TunnelName = "elabs-prod",
    [string]$ConfigPath = "$PSScriptRoot\config.yml"
)

$ErrorActionPreference = "Stop"

# ── 1. Download cloudflared if not installed ──────────────────────────────────
$cfBin = "C:\cloudflared\cloudflared.exe"
if (-not (Test-Path $cfBin)) {
    Write-Host "[1/5] Downloading cloudflared..." -ForegroundColor Cyan
    New-Item -ItemType Directory -Force -Path "C:\cloudflared" | Out-Null
    $url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    Invoke-WebRequest -Uri $url -OutFile $cfBin -UseBasicParsing
    Write-Host "     Saved to $cfBin" -ForegroundColor Green
} else {
    Write-Host "[1/5] cloudflared already present at $cfBin" -ForegroundColor Green
}

# Add to PATH for this session
$env:PATH = "C:\cloudflared;$env:PATH"

# ── 2. Authenticate with Cloudflare (opens browser) ───────────────────────────
Write-Host ""
Write-Host "[2/5] Authenticating with Cloudflare..." -ForegroundColor Cyan
Write-Host "      A browser window will open. Log in and authorize elabsai.com."
& $cfBin tunnel login

# ── 3. Create the tunnel ──────────────────────────────────────────────────────
Write-Host ""
Write-Host "[3/5] Creating tunnel '$TunnelName'..." -ForegroundColor Cyan
$createOutput = & $cfBin tunnel create $TunnelName 2>&1
Write-Host $createOutput

# Extract UUID from output
$uuid = ($createOutput | Select-String -Pattern '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}').Matches.Value | Select-Object -First 1
if (-not $uuid) {
    # Tunnel may already exist — list and grab UUID
    $listOutput = & $cfBin tunnel list --output json 2>&1
    $tunnelData = $listOutput | ConvertFrom-Json
    $uuid = ($tunnelData | Where-Object { $_.name -eq $TunnelName }).id
}
if (-not $uuid) {
    Write-Error "Could not determine tunnel UUID. Run: cloudflared tunnel list"
    exit 1
}
Write-Host "     Tunnel UUID: $uuid" -ForegroundColor Green

# ── 4. Patch config.yml with real UUID ────────────────────────────────────────
Write-Host ""
Write-Host "[4/5] Updating config.yml with tunnel UUID..." -ForegroundColor Cyan
$configContent = Get-Content $ConfigPath -Raw
$configContent = $configContent -replace 'REPLACE_WITH_TUNNEL_UUID', $uuid
Set-Content -Path $ConfigPath -Value $configContent -NoNewline
Write-Host "     config.yml updated." -ForegroundColor Green

# ── 5. Create DNS CNAME records ───────────────────────────────────────────────
Write-Host ""
Write-Host "[5/5] Creating DNS CNAME records for elabsai.com..." -ForegroundColor Cyan
$subdomains = @("www", "copilot", "machine", "api", "gateway", "enterprise", "gpu")
foreach ($sub in $subdomains) {
    Write-Host "     Adding $sub.elabsai.com -> $uuid.cfargotunnel.com"
    & $cfBin tunnel route dns $TunnelName "$sub.elabsai.com" 2>&1
}

# ── Done ──────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "========================================================" -ForegroundColor Green
Write-Host "  Tunnel ready!  UUID: $uuid" -ForegroundColor Green
Write-Host "========================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  To start the tunnel manually:"
Write-Host "    cloudflared tunnel --config `"$ConfigPath`" run" -ForegroundColor Yellow
Write-Host ""
Write-Host "  To install as a Windows service (auto-start on reboot):"
Write-Host "    cloudflared service install --config `"$ConfigPath`"" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Your endpoints (once DNS propagates, usually < 5 min):"
Write-Host "    https://www.elabsai.com          -> Marketing + pricing landing page"
Write-Host "    https://copilot.elabsai.com      -> E-Labs Copilot WebUI + API"
Write-Host "    https://machine.elabsai.com      -> THE MACHINE workflows"
Write-Host "    https://api.elabsai.com           -> OpenClaw developer API"
Write-Host "    https://gateway.elabsai.com      -> OpenClaw (compat alias)"
Write-Host "    https://enterprise.elabsai.com   -> Enterprise early access"
Write-Host "    https://gpu.elabsai.com           -> GPU Rental Platform"
Write-Host ""
