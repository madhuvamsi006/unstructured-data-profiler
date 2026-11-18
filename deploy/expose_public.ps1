<#
.SYNOPSIS
  Put a public HTTPS load balancer in front of the Cloud Run demo.

.DESCRIPTION
  Cloud Run itself cannot be public here: the constraints/run.allowedIngress
  org policy permits only "internal" and "internal-and-cloud-load-balancing".
  The supported way to reach it from the internet is therefore an external
  HTTPS load balancer, which is also how this project's other public services
  are built (serverless NEG -> backend -> URL map -> HTTPS proxy -> rule).

  No domain is needed: the managed certificate is issued for <IP>.nip.io,
  which resolves to the load balancer's own address. That mirrors the existing
  agentic-travel and gs-api services in this project.

  Idempotent - re-run it safely.

.EXAMPLE
  .\deploy\expose_public.ps1
  .\deploy\expose_public.ps1 -Service estate-profiler-demo -Region us-central1
#>

[CmdletBinding()]
param(
    [string]$Project = "",
    [string]$Region = "us-central1",
    [string]$Service = "estate-profiler-demo",
    [string]$Prefix = "estate-profiler"
)

# gcloud writes progress to stderr; "Stop" would abort on non-errors.
$ErrorActionPreference = "Continue"

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Note($text) { Write-Host "    $text" -ForegroundColor DarkGray }

function Invoke-Gcloud {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $output = & gcloud @Arguments 2>&1 | ForEach-Object { $_.ToString() }
    $script:GcloudExit = $LASTEXITCODE
    return ($output -join "`n")
}

function Fail($message, $hint) {
    Write-Host "`n$message" -ForegroundColor Red
    if ($hint) { Write-Host "  $hint" -ForegroundColor Yellow }
    exit 1
}

if (-not $Project) { $Project = (Invoke-Gcloud config get-value project).Trim() }
if (-not $Project -or $Project -eq "(unset)") { Fail "No project set." "gcloud config set project <id>" }

$IpName      = "$Prefix-ip"
$NegName     = "$Prefix-neg"
$BackendName = "$Prefix-backend"
$UrlMapName  = "$Prefix-urlmap"
$CertName    = "$Prefix-cert"
$ProxyName   = "$Prefix-proxy"
$RuleName    = "$Prefix-fr"

Note "project : $Project"
Note "service : $Service ($Region)"

# --- 1. static IP ----------------------------------------------------------
Step "Reserving a global static IP"
$null = Invoke-Gcloud compute addresses describe $IpName --project $Project --global --format "value(address)"
if ($GcloudExit -ne 0) {
    $out = Invoke-Gcloud compute addresses create $IpName --project $Project --global --ip-version IPV4
    if ($GcloudExit -ne 0) {
        Fail "Could not reserve a static IP.`n$out" "You may lack compute.addresses.create on $Project"
    }
}
$Ip = (Invoke-Gcloud compute addresses describe $IpName --project $Project --global --format "value(address)").Trim()
if (-not $Ip) { Fail "Reserved the address but could not read it back." }
$Domain = "$Ip.nip.io"
Note "ip     : $Ip"
Note "domain : $Domain  (nip.io resolves this to the IP - no DNS to configure)"

# --- 2. serverless NEG -----------------------------------------------------
Step "Creating the serverless network endpoint group"
$null = Invoke-Gcloud compute network-endpoint-groups describe $NegName --project $Project --region $Region
if ($GcloudExit -ne 0) {
    $out = Invoke-Gcloud compute network-endpoint-groups create $NegName `
        --project $Project --region $Region `
        --network-endpoint-type serverless --cloud-run-service $Service
    if ($GcloudExit -ne 0) { Fail "Could not create the NEG.`n$out" }
    Note "created $NegName"
} else { Note "exists: $NegName" }

# --- 3. backend service ----------------------------------------------------
Step "Creating the backend service"
$null = Invoke-Gcloud compute backend-services describe $BackendName --project $Project --global
if ($GcloudExit -ne 0) {
    $out = Invoke-Gcloud compute backend-services create $BackendName `
        --project $Project --global --load-balancing-scheme EXTERNAL_MANAGED
    if ($GcloudExit -ne 0) { Fail "Could not create the backend service.`n$out" }
    Note "created $BackendName"
} else { Note "exists: $BackendName" }

$backends = Invoke-Gcloud compute backend-services describe $BackendName `
    --project $Project --global --format "value(backends[].group)"
if ($backends -notmatch [regex]::Escape($NegName)) {
    $out = Invoke-Gcloud compute backend-services add-backend $BackendName `
        --project $Project --global `
        --network-endpoint-group $NegName --network-endpoint-group-region $Region
    if ($GcloudExit -ne 0) { Fail "Could not attach the NEG to the backend.`n$out" }
    Note "attached $NegName"
} else { Note "NEG already attached" }

# --- 4. URL map ------------------------------------------------------------
Step "Creating the URL map"
$null = Invoke-Gcloud compute url-maps describe $UrlMapName --project $Project --global
if ($GcloudExit -ne 0) {
    $out = Invoke-Gcloud compute url-maps create $UrlMapName `
        --project $Project --global --default-service $BackendName
    if ($GcloudExit -ne 0) { Fail "Could not create the URL map.`n$out" }
    Note "created $UrlMapName"
} else { Note "exists: $UrlMapName" }

# --- 5. managed certificate ------------------------------------------------
Step "Creating the managed TLS certificate for $Domain"
$null = Invoke-Gcloud compute ssl-certificates describe $CertName --project $Project --global
if ($GcloudExit -ne 0) {
    $out = Invoke-Gcloud compute ssl-certificates create $CertName `
        --project $Project --global --domains $Domain
    if ($GcloudExit -ne 0) { Fail "Could not create the certificate.`n$out" }
    Note "created $CertName"
} else { Note "exists: $CertName" }

# --- 6. HTTPS proxy + forwarding rule --------------------------------------
Step "Creating the HTTPS proxy and forwarding rule"
$null = Invoke-Gcloud compute target-https-proxies describe $ProxyName --project $Project --global
if ($GcloudExit -ne 0) {
    $out = Invoke-Gcloud compute target-https-proxies create $ProxyName `
        --project $Project --global --url-map $UrlMapName --ssl-certificates $CertName
    if ($GcloudExit -ne 0) { Fail "Could not create the HTTPS proxy.`n$out" }
    Note "created $ProxyName"
} else { Note "exists: $ProxyName" }

$null = Invoke-Gcloud compute forwarding-rules describe $RuleName --project $Project --global
if ($GcloudExit -ne 0) {
    $out = Invoke-Gcloud compute forwarding-rules create $RuleName `
        --project $Project --global `
        --address $IpName --target-https-proxy $ProxyName --ports 443 `
        --load-balancing-scheme EXTERNAL_MANAGED
    if ($GcloudExit -ne 0) { Fail "Could not create the forwarding rule.`n$out" }
    Note "created $RuleName"
} else { Note "exists: $RuleName" }

# --- done ------------------------------------------------------------------
$certState = (Invoke-Gcloud compute ssl-certificates describe $CertName `
    --project $Project --global --format "value(managed.status)").Trim()

Write-Host "`n==================================================================" -ForegroundColor Green
Write-Host " Public URL:  https://$Domain" -ForegroundColor Green
Write-Host "==================================================================" -ForegroundColor Green
Write-Host "`n Certificate status: $certState"
if ($certState -ne "ACTIVE") {
    Write-Host " Google-managed certificates take 10-30 minutes to provision." -ForegroundColor Yellow
    Write-Host " Until it is ACTIVE the URL will fail to connect. Check with:" -ForegroundColor Yellow
    Write-Host "   gcloud compute ssl-certificates describe $CertName --project $Project --global --format='value(managed.status)'" -ForegroundColor Cyan
}
Write-Host "`n This URL is PUBLIC - no sign-in. Scannable sources are limited by"
Write-Host " PROFILER_ALLOWED_SOURCES on the Cloud Run service. Verify with:"
Write-Host "   python deploy\verify_deployment.py --url https://$Domain" -ForegroundColor Cyan
Write-Host "`n To take it down:  .\deploy\teardown_public.ps1" -ForegroundColor DarkGray
Write-Host ""
