<#
.SYNOPSIS
  Open an authenticated tunnel to the private Cloud Run demo.

.DESCRIPTION
  The service is deployed private, so it cannot be opened straight from a
  browser — a browser sends no identity token. `gcloud run services proxy`
  runs a local listener that attaches your credentials to every request, so
  http://127.0.0.1:8080 behaves exactly like the hosted service.

  Leave this window open for the duration of the demo.

.EXAMPLE
  .\deploy\proxy.ps1
  .\deploy\proxy.ps1 -Port 9000
#>

[CmdletBinding()]
param(
    [string]$Project = "",
    [string]$Region = "asia-southeast1",
    [string]$Service = "estate-profiler-demo",
    [int]$Port = 8080
)

# See deploy.ps1: "Stop" plus gcloud's stderr-as-progress aborts on non-errors.
$ErrorActionPreference = "Continue"

if (-not $Project) {
    $Project = (& gcloud config get-value project 2>&1 | ForEach-Object { $_.ToString() }).Trim()
}

$token = & gcloud auth print-access-token 2>&1 | ForEach-Object { $_.ToString() }
if ($LASTEXITCODE -ne 0 -or $token -notmatch "ya29|^ey") {
    Write-Host "Not signed in to gcloud. Run: gcloud auth login" -ForegroundColor Red
    exit 1
}

# Something already on the port means a stale proxy or the local demo server.
$inUse = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($inUse) {
    Write-Host "Port $Port is already in use (local demo server, or an old proxy)." -ForegroundColor Yellow
    Write-Host "Either stop it, or re-run with:  .\deploy\proxy.ps1 -Port 9000"
    exit 1
}

Write-Host "`nTunnelling to $Service ($Region) in $Project" -ForegroundColor Cyan
Write-Host "  -> http://127.0.0.1:$Port" -ForegroundColor Green
Write-Host "  Leave this window open. Ctrl+C to stop.`n" -ForegroundColor DarkGray

gcloud run services proxy $Service --project $Project --region $Region --port $Port
