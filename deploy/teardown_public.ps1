<#
.SYNOPSIS
  Remove the public load balancer, taking the demo off the internet.

.DESCRIPTION
  Deletes the forwarding rule, HTTPS proxy, certificate, URL map, backend
  service, NEG and static IP created by expose_public.ps1 - in dependency
  order, since GCP refuses to delete a resource another still references.

  The Cloud Run service itself is left alone; it stays reachable internally.
  Pass -AlsoMakePrivate to additionally revoke public invocation on it.

.EXAMPLE
  .\deploy\teardown_public.ps1
  .\deploy\teardown_public.ps1 -AlsoMakePrivate
#>

[CmdletBinding()]
param(
    [string]$Project = "",
    [string]$Region = "us-central1",
    [string]$Service = "estate-profiler-demo",
    [string]$Prefix = "estate-profiler",
    [switch]$AlsoMakePrivate,
    [switch]$Force
)

$ErrorActionPreference = "Continue"

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Note($text) { Write-Host "    $text" -ForegroundColor DarkGray }

function Invoke-Gcloud {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $output = & gcloud @Arguments 2>&1 | ForEach-Object { $_.ToString() }
    $script:GcloudExit = $LASTEXITCODE
    return ($output -join "`n")
}

if (-not $Project) { $Project = (Invoke-Gcloud config get-value project).Trim() }

if (-not $Force) {
    Write-Host "`nThis removes the public endpoint for '$Service' in $Project." -ForegroundColor Yellow
    $answer = Read-Host "Type 'yes' to continue"
    if ($answer -ne "yes") { Write-Host "Cancelled."; exit 0 }
}

# Reverse dependency order - a resource cannot go while something points at it.
$steps = @(
    @{ Label = "forwarding rule"; Args = @("compute", "forwarding-rules", "delete", "$Prefix-fr", "--global") },
    @{ Label = "HTTPS proxy";     Args = @("compute", "target-https-proxies", "delete", "$Prefix-proxy", "--global") },
    @{ Label = "certificate";     Args = @("compute", "ssl-certificates", "delete", "$Prefix-cert", "--global") },
    @{ Label = "URL map";         Args = @("compute", "url-maps", "delete", "$Prefix-urlmap", "--global") },
    @{ Label = "backend service"; Args = @("compute", "backend-services", "delete", "$Prefix-backend", "--global") },
    @{ Label = "serverless NEG";  Args = @("compute", "network-endpoint-groups", "delete", "$Prefix-neg", "--region", $Region) },
    @{ Label = "static IP";       Args = @("compute", "addresses", "delete", "$Prefix-ip", "--global") }
)

foreach ($step in $steps) {
    Step "Deleting $($step.Label)"
    $out = Invoke-Gcloud @($step.Args + @("--project", $Project, "--quiet"))
    if ($GcloudExit -eq 0) {
        Note "deleted"
    } elseif ($out -match "was not found|NOT_FOUND") {
        Note "already gone"
    } else {
        Write-Host "    could not delete: $out" -ForegroundColor Yellow
    }
}

if ($AlsoMakePrivate) {
    Step "Revoking public invocation on the Cloud Run service"
    $out = Invoke-Gcloud run services remove-iam-policy-binding $Service `
        --project $Project --region $Region `
        --member allUsers --role roles/run.invoker --quiet
    if ($GcloudExit -eq 0) { Note "allUsers removed" } else { Note "could not remove: $out" }
}

Write-Host "`nPublic endpoint removed. The Cloud Run service itself is untouched." -ForegroundColor Green
Write-Host ""
