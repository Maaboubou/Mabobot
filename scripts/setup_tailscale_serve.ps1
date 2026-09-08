[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8888,
    [switch]$SkipHealthCheck
)

$ErrorActionPreference = 'Stop'

$tailscaleCommand = Get-Command tailscale.exe -ErrorAction SilentlyContinue
$tailscalePath = if ($tailscaleCommand) { $tailscaleCommand.Source } else { $null }
if (-not $tailscalePath) {
    $candidate = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
    if (Test-Path $candidate) {
        $tailscalePath = $candidate
    }
}
if (-not $tailscalePath) {
    throw 'tailscale.exe was not found. Install Tailscale and sign in on this machine first.'
}

$localUrl = "http://127.0.0.1:$Port"
if (-not $SkipHealthCheck) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "$localUrl/health" -TimeoutSec 5
        if ($response.StatusCode -lt 200 -or $response.StatusCode -ge 500) {
            throw "HTTP $($response.StatusCode)"
        }
    }
    catch {
        throw "The local service is not ready at $localUrl/health. Start Mabobot first, or explicitly use -SkipHealthCheck. Details: $($_.Exception.Message)"
    }
}

Write-Host "Configuring this Tailscale node's HTTPS Serve endpoint for $localUrl ..."
& $tailscalePath serve --bg --yes $localUrl
if ($LASTEXITCODE -ne 0) {
    throw "Tailscale HTTPS Serve configuration failed with exit code $LASTEXITCODE. If this is the first Serve endpoint in the tailnet, open the approval URL printed by Tailscale and then run this script again."
}

Write-Host "Adding the tailnet-only HTTP endpoint on port $Port ..."
& $tailscalePath serve --bg --yes --http=$Port $localUrl
if ($LASTEXITCODE -ne 0) {
    throw "Tailscale HTTP Serve configuration failed with exit code $LASTEXITCODE. The HTTPS endpoint might already be active; run 'tailscale serve status' to inspect it."
}

Write-Host ''
Write-Host 'Current Serve status:'
& $tailscalePath serve status
if ($LASTEXITCODE -ne 0) {
    throw "Unable to read Tailscale Serve status; tailscale.exe exited with code $LASTEXITCODE."
}

Write-Host ''
Write-Host "Configuration complete. Remote devices can use https://<machine-name>.<tailnet>.ts.net or http://<machine-name>:$Port."
Write-Host 'This script does not enable Tailscale Funnel or change any tailnet access policy.'
