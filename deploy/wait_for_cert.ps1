<#
.SYNOPSIS
  Wait for the managed certificate to become ACTIVE, then report the URL.

.DESCRIPTION
  Google-managed certificates take 10-30 minutes to provision, and the public
  URL simply refuses connections until then - which looks like a broken deploy
  if you do not know to expect it. This polls until it is ready.
#>

[CmdletBinding()]
param(
    [string]$Project = "",
    [string]$CertName = "estate-profiler-cert",
    [int]$TimeoutMinutes = 45
)

$ErrorActionPreference = "Continue"

function Invoke-Gcloud {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $output = & gcloud @Arguments 2>&1 | ForEach-Object { $_.ToString() }
    $script:GcloudExit = $LASTEXITCODE
    return ($output -join "`n")
}

if (-not $Project) { $Project = (Invoke-Gcloud config get-value project).Trim() }

$domain = (Invoke-Gcloud compute ssl-certificates describe $CertName `
    --project $Project --global --format "value(managed.domains[0])").Trim()

Write-Host "Waiting for certificate '$CertName' ($domain) ..." -ForegroundColor Cyan
$deadline = (Get-Date).AddMinutes($TimeoutMinutes)
$last = ""

while ((Get-Date) -lt $deadline) {
    $status = (Invoke-Gcloud compute ssl-certificates describe $CertName `
        --project $Project --global --format "value(managed.status)").Trim()

    if ($status -ne $last) {
        Write-Host ("  [{0:HH:mm:ss}] {1}" -f (Get-Date), $status) -ForegroundColor DarkGray
        $last = $status
    }

    if ($status -eq "ACTIVE") {
        Write-Host "`nCertificate is ACTIVE." -ForegroundColor Green
        Write-Host "  https://$domain" -ForegroundColor Green
        exit 0
    }
    if ($status -eq "FAILED_NOT_VISIBLE") {
        Write-Host "`nProvisioning failed: the domain does not resolve to the load balancer." -ForegroundColor Red
        exit 1
    }
    Start-Sleep -Seconds 30
}

Write-Host "`nStill '$last' after $TimeoutMinutes minutes." -ForegroundColor Yellow
Write-Host "That is unusual but not necessarily fatal - check again with:" -ForegroundColor Yellow
Write-Host "  gcloud compute ssl-certificates describe $CertName --project $Project --global --format='value(managed.status)'"
exit 1
