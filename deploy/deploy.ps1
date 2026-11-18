<#
.SYNOPSIS
  Deploy the Unstructured Data Profiler demo console to Cloud Run.

.DESCRIPTION
  Idempotent: safe to re-run. Enables the APIs, creates a least-privilege
  runtime service account, grants it what the agent actually uses, and deploys
  from source via Cloud Build (so no local Docker is required).

  The service is deployed PRIVATE (--no-allow-unauthenticated). Reach it with
  `deploy\proxy.ps1`, which opens an authenticated tunnel on localhost.

.EXAMPLE
  .\deploy\deploy.ps1
  .\deploy\deploy.ps1 -Project ctoteam -Region asia-southeast1
#>

[CmdletBinding()]
param(
    [string]$Project = "",
    # us-central1 by default: it is where this project's other Cloud Run
    # services already live, and it co-locates the app with the Vertex region.
    [string]$Region = "us-central1",
    [string]$Service = "estate-profiler-demo",
    [string]$VertexLocation = "us-central1",
    [string]$Model = "gemini-2.5-flash",
    # Use an EXISTING service account instead of creating one. Needed when you
    # lack iam.serviceAccounts.create - the account must already hold
    # aiplatform.user and (for DLP) dlp.user.
    [string]$ServiceAccountEmail = "",
    # Deploy with the project's default compute service account. Simplest when
    # you can create nothing, but that account is usually broadly privileged.
    [switch]$UseDefaultServiceAccount,
    # Network exposure. Many organisations forbid public Cloud Run via the
    # constraints/run.allowedIngress org policy; this project allows only
    # "internal" and "internal-and-cloud-load-balancing", and its existing
    # services use the latter, so that is the default here.
    [ValidateSet("all", "internal", "internal-and-cloud-load-balancing")]
    [string]$Ingress = "internal-and-cloud-load-balancing",
    # Comma-separated sources this deployment may scan. REQUIRED in spirit for
    # any publicly reachable deployment: without it a visitor can type any
    # bucket name and have the runtime service account enumerate it.
    [string]$AllowedSources = "/app/sample_estate",
    # Keep one instance warm. Costs a few dollars a month and removes the
    # 20-second cold start that makes a live demo look broken.
    [int]$MinInstances = 0,
    [switch]$AllowUnauthenticated
)

# Deliberately NOT "Stop". gcloud writes ordinary progress to stderr, and in
# Windows PowerShell a redirected native stderr line becomes a NativeCommandError
# record - which under "Stop" aborts the script on messages that are not errors
# at all (a service-account lookup returning NOT_FOUND is the expected answer
# the first time you deploy). Every gcloud call below checks $LASTEXITCODE.
$ErrorActionPreference = "Continue"
$RepoRoot = Split-Path -Parent $PSScriptRoot

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Note($text) { Write-Host "    $text" -ForegroundColor DarkGray }

function Invoke-Gcloud {
    <#  Run gcloud, capture combined output as plain strings, return the exit
        code in $script:GcloudExit. Never throws. #>
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

# --- preflight -------------------------------------------------------------
Step "Checking gcloud authentication"
$token = Invoke-Gcloud auth print-access-token
if ($GcloudExit -ne 0 -or $token -notmatch "ya29|^ey") {
    Fail "Not signed in to gcloud." "Run:  gcloud auth login"
}
Note "signed in"

if (-not $Project) {
    $Project = (Invoke-Gcloud config get-value project).Trim()
    if (-not $Project -or $Project -eq "(unset)") {
        Fail "No project set." "Pass -Project <id>, or run: gcloud config set project <id>"
    }
}
$ServiceAccount = "$Service-sa"
$ManageServiceAccount = (-not $ServiceAccountEmail) -and (-not $UseDefaultServiceAccount)
if ($ManageServiceAccount) {
    $ServiceAccountEmail = "$ServiceAccount@$Project.iam.gserviceaccount.com"
}

Note "project : $Project"
Note "region  : $Region"
Note "service : $Service"
Note "vertex  : $VertexLocation / $Model"
Note "ingress : $Ingress"
if ($UseDefaultServiceAccount) {
    Note "runtime : project default compute service account"
} else {
    Note "runtime : $ServiceAccountEmail$(if (-not $ManageServiceAccount) { ' (existing, not managed here)' })"
}

# --- APIs ------------------------------------------------------------------
Step "Enabling required APIs (no-op if already enabled)"
$apis = @(
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "aiplatform.googleapis.com",
    "dlp.googleapis.com",
    "bigquery.googleapis.com",
    "storage.googleapis.com"
)
$out = Invoke-Gcloud services enable @apis --project $Project
if ($GcloudExit -ne 0) { Fail "Could not enable APIs.`n$out" "You may lack serviceusage.services.enable on $Project" }
Note "enabled"

# --- runtime service account ----------------------------------------------
if ($UseDefaultServiceAccount) {
    Step "Runtime identity"
    Note "using the project default compute service account"
    Note "it is usually broadly privileged - prefer a dedicated account where you can"
} elseif (-not $ManageServiceAccount) {
    Step "Runtime identity"
    $out = Invoke-Gcloud iam service-accounts describe $ServiceAccountEmail `
        --project $Project --format "value(email)"
    if ($GcloudExit -ne 0) { Fail "Service account not found: $ServiceAccountEmail`n$out" }
    Note "using existing $ServiceAccountEmail"
    Note "assuming it already holds aiplatform.user (and dlp.user for DLP)"
} else {
    Step "Ensuring runtime service account"
    # NOT_FOUND here is the normal first-run answer, not a failure.
    $out = Invoke-Gcloud iam service-accounts describe $ServiceAccountEmail --project $Project
    if ($GcloudExit -ne 0) {
        $out = Invoke-Gcloud iam service-accounts create $ServiceAccount `
            --project $Project --display-name "Estate Profiler demo runtime"
        if ($GcloudExit -ne 0) {
            Write-Host "`nCannot create a service account on $Project." -ForegroundColor Red
            Write-Host $out -ForegroundColor DarkGray
            Write-Host "`nYou lack iam.serviceAccounts.create. Three ways forward:" -ForegroundColor Yellow
            Write-Host "  1. Reuse an existing account that already has Vertex access:" -ForegroundColor White
            Write-Host "       .\deploy\deploy.ps1 -ServiceAccountEmail <name>@$Project.iam.gserviceaccount.com" -ForegroundColor Cyan
            Write-Host "     List candidates with:" -ForegroundColor White
            Write-Host "       gcloud iam service-accounts list --project $Project" -ForegroundColor Cyan
            Write-Host "  2. Use the project default compute account:" -ForegroundColor White
            Write-Host "       .\deploy\deploy.ps1 -UseDefaultServiceAccount" -ForegroundColor Cyan
            Write-Host "  3. Ask an admin for roles/iam.serviceAccountAdmin on $Project." -ForegroundColor White
            exit 1
        }
        Note "created $ServiceAccountEmail"
        # IAM propagation lags creation; the role bindings below can 400 without it.
        Start-Sleep -Seconds 10
    } else {
        Note "exists: $ServiceAccountEmail"
    }

    Step "Granting roles the agent actually uses"
    # Deliberately narrow. Storage is read-only: the profiler reads estates, it
    # never writes to them. Add roles/storage.objectAdmin only if you later want
    # run artifacts persisted back to a bucket.
    $roles = @(
        "roles/aiplatform.user",        # Vertex AI - the agent's model
        "roles/dlp.user",               # Cloud DLP - sensitivity escalation
        "roles/storage.objectViewer",   # read gs:// estates
        "roles/bigquery.dataEditor",    # write the inventory table
        "roles/bigquery.jobUser"        # run the load job
    )
    foreach ($role in $roles) {
        $out = Invoke-Gcloud projects add-iam-policy-binding $Project `
            --member "serviceAccount:$ServiceAccountEmail" `
            --role $role --condition=None --quiet --format "value(etag)"
        if ($GcloudExit -ne 0) {
            Fail "Could not grant $role.`n$out" "You may lack resourcemanager.projects.setIamPolicy on $Project"
        }
        Note "granted $role"
    }
}

# --- deploy ----------------------------------------------------------------
Step "Building and deploying from source (Cloud Build)"
Note "this takes 3-5 minutes on a first deploy"

$envVars = @(
    "GOOGLE_GENAI_USE_VERTEXAI=TRUE",
    "GOOGLE_CLOUD_PROJECT=$Project",
    "GOOGLE_CLOUD_LOCATION=$VertexLocation",
    "PROFILER_MODEL=$Model",
    "PROFILER_RUN_ROOT=/tmp/runs",
    "PROFILER_USE_DLP=false",
    "PROFILER_BQ_DATASET=estate_profiler",
    "PROFILER_BQ_TABLE=file_inventory",
    "PROFILER_ALLOWED_SOURCES=$AllowedSources"
) -join "|"

$authFlag = if ($AllowUnauthenticated) { "--allow-unauthenticated" } else { "--no-allow-unauthenticated" }
if ($AllowUnauthenticated) {
    if (-not $AllowedSources) {
        Write-Host "`nRefusing to deploy publicly with no source allowlist." -ForegroundColor Red
        Write-Host "  A visitor could type any bucket name and have the runtime service" -ForegroundColor Yellow
        Write-Host "  account enumerate it. Pass -AllowedSources '<path or gs:// prefix>'." -ForegroundColor Yellow
        exit 1
    }
    Write-Host "    PUBLIC deployment - scannable sources limited to: $AllowedSources" -ForegroundColor Yellow
}

$identityArgs = @()
if (-not $UseDefaultServiceAccount) {
    $identityArgs = @("--service-account", $ServiceAccountEmail)
}

# Streamed, not captured: the build log is the useful thing to watch here.
& gcloud run deploy $Service `
    --source $RepoRoot `
    --project $Project `
    --region $Region `
    @identityArgs `
    --set-env-vars "^|^$envVars" `
    --memory 2Gi `
    --cpu 2 `
    --timeout 900 `
    --concurrency 20 `
    --min-instances $MinInstances `
    --max-instances 3 `
    --ingress $Ingress `
    $authFlag `
    --quiet
$deployExit = $LASTEXITCODE

if ($deployExit -ne 0) {
    Fail "Deploy failed (exit $deployExit)." "Scroll up for the Cloud Build log; it names the failing step."
}

$url = (Invoke-Gcloud run services describe $Service --project $Project --region $Region --format "value(status.url)").Trim()
if ($GcloudExit -ne 0 -or -not $url) { Fail "Deployed, but could not read the service URL." }

Write-Host "`n==================================================================" -ForegroundColor Green
Write-Host " Deployed: $url" -ForegroundColor Green
Write-Host "==================================================================" -ForegroundColor Green

if ($AllowUnauthenticated) {
    Write-Host "`nOpen it directly - it is public."
} else {
    Write-Host "`nThe service is private. Open an authenticated tunnel:" -ForegroundColor White
    Write-Host "    .\deploy\proxy.ps1" -ForegroundColor Cyan
    Write-Host "  then browse to http://127.0.0.1:8080"
}
Write-Host "`nVerify the deployment end to end:"
Write-Host "    .\deploy\verify.ps1" -ForegroundColor Cyan
Write-Host ""
