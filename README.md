# Unstructured Data Profiler Agent

An ADK agent that crawls and triages an unstructured data **estate** — file
shares, mounted drives, GCS buckets — and reports what it would take to migrate
it or make it RAG-ready.

## Why this exists

Document AI parses an individual document. Vertex AI Search indexes content
that is already fit to index. Neither answers the question that actually gates a
migration deal:

> We have 40 TB on a file share nobody has looked at since 2019. What is in it,
> how much of it is junk, how much is a compliance problem, and how much of it
> could realistically feed a RAG pipeline?

This agent answers that at estate scale, before anyone commits budget.

## What it does

| Pass | Output |
|---|---|
| **Inventory** | File counts, bytes, types, size/age distribution, folder hotspots |
| **Format health** | Magic-byte sniffing — encrypted, corrupt, zero-byte, legacy, renamed, archived, unprocessable |
| **Deduplication** | Exact duplicates by content hash; near-duplicates by shingle similarity, with reclaimable bytes |
| **Sensitivity** | PII / PHI / financial / credential detection, checksum-validated, optional Cloud DLP escalation |
| **Readiness** | A 0–100 Document AI / RAG-readiness score per file, a disposition, and a recommended processor |

Every file lands in exactly one **disposition**:

| Disposition | Meaning |
|---|---|
| `ingest_now` | Clean, unique, low-risk — ready for the pipeline as-is |
| `ingest_after_processing` | Needs Document AI OCR, format conversion, or archive expansion |
| `remediate` | Fix sensitivity or a format problem first |
| `quarantine` | Credentials, encrypted, or corrupt — hold out of the corpus |
| `exclude` | Duplicates and non-documents |

## Quick start — the demo console

```bash
pip install -r requirements.txt
cp .env.example .env                              # set GOOGLE_CLOUD_PROJECT
python tools/make_sample_estate.py sample_estate  # synthetic estate to scan
gcloud auth application-default login             # so the agent can reach Vertex AI

python demo/server.py                             # → http://127.0.0.1:8080
```

That is the thing to put in front of a client. One screen:

- **Run a scan** against a bucket or a folder, with the crawl streaming live
- **Dashboard** renders inline the moment the scan lands
- **Ask the agent** in natural language, with each tool call shown as it fires —
  so the audience sees `scan_unstructured_estate`, `query_estate_files` and
  `build_estate_dashboard` actually running, not a black box
- **Report .md** and **Manifest .jsonl** download straight from the toolbar

The console degrades honestly: if the agent cannot reach a model it says so,
shows the exact command to fix it, and scanning, the dashboard and the report
all keep working.

### Demo script that lands

1. Open on the sample estate, hit **Run scan** — it finishes in under a second
   and the credential finding turns the KPI red.
2. Switch to **Dashboard**, scroll to *What happens to every file*.
3. Switch to **Ask the agent**: *"Which files are quarantined, and why?"* —
   the tool chips show it querying the manifest rather than guessing.
4. Finish on *"Summarise this estate for a migration steering group."*

## Hosted on Cloud Run

For demos without carrying a laptop setup. Deploys **private** — only you can
reach it.

```powershell
gcloud auth login
.\deploy\deploy.ps1                  # ~4 min first time, builds via Cloud Build
.\deploy\proxy.ps1                   # authenticated tunnel
#   -> http://127.0.0.1:8080
python deploy\verify_deployment.py   # proves it actually works
```

No local Docker needed — `gcloud run deploy --source .` builds in Cloud Build.

The deploy script is idempotent. By default it creates a least-privilege
runtime service account and grants it only what the agent uses:
`aiplatform.user`, `dlp.user`, `storage.objectViewer` (read-only — the profiler
never writes to an estate), `bigquery.dataEditor` and `bigquery.jobUser`.

### When your project is locked down

Corporate projects usually are, and the script has a switch for each case.

**You cannot create a service account** (`iam.serviceAccounts.create` denied) —
reuse one that already has Vertex access, or fall back to the default compute
account:

```powershell
gcloud iam service-accounts list --project <project>
.\deploy\deploy.ps1 -ServiceAccountEmail existing-sa@<project>.iam.gserviceaccount.com
.\deploy\deploy.ps1 -UseDefaultServiceAccount
```

With either, the script skips role granting — the account must *already* hold
`roles/aiplatform.user`, plus `roles/dlp.user` if you want DLP escalation.

**Public ingress is blocked** by the `constraints/run.allowedIngress` org
policy. Check what yours permits:

```powershell
gcloud resource-manager org-policies describe constraints/run.allowedIngress --project <project> --effective
```

The script defaults to `internal-and-cloud-load-balancing`, which most policies
allow. Override with `-Ingress internal` or `-Ingress all`.

Restricted ingress means **the `*.run.app` URL is not reachable from a laptop**
— traffic has to originate inside the VPC or arrive through a Cloud Load
Balancer. That is a real constraint on the "hosted demo" idea, not a
configuration detail: without an external load balancer (and ideally IAP) in
front, the hosted service is reachable only from inside the network. Running
the console locally (`python demo/server.py`) stays the simplest demo path.

### What is different when hosted

| | Local | Cloud Run |
|---|---|---|
| Local paths and file shares | ✅ | ❌ no filesystem to mount |
| `gs://` buckets | ✅ | ✅ |
| Bundled sample estate | ✅ | ✅ baked into the image |
| Run history | persists in `runs/` | **ephemeral** — resets when the instance recycles |
| Credentials | your ADC | the service account, no expiry |

The console detects this and adapts: it drops the local-path preset and says
plainly that run history resets. **BigQuery is the durable record** if a hosted
run needs to outlive the instance.

One genuine upside of hosting: no more expired ADC five minutes before a call —
the service account doesn't lapse.

### Making it public

Cloud Run cannot be public directly under the `run.allowedIngress` org policy.
The supported route — and the pattern this project's other public services
already use — is an external HTTPS load balancer in front of it:

```powershell
.\deploy\deploy.ps1 -ServiceAccountEmail <sa>@<project>.iam.gserviceaccount.com `
                    -AllowUnauthenticated -AllowedSources "/app/sample_estate"
.\deploy\expose_public.ps1
#   -> https://<ip>.nip.io     (10-30 min for the certificate to go ACTIVE)
.\deploy\teardown_public.ps1   # removes it again
```

No domain is required: the managed certificate is issued for `<IP>.nip.io`,
which resolves to the load balancer's own address.

**The allowlist is what makes public acceptable.** The console lets whoever
opens it type any source they like. Unrestricted, a public URL would let
anyone enumerate every bucket the runtime service account can read, and spend
your Vertex and DLP budget. So `PROFILER_ALLOWED_SOURCES` confines scanning to
named prefixes:

```bash
PROFILER_ALLOWED_SOURCES=/app/sample_estate,gs://demo-estate/public
```

Empty means unrestricted, which is right on a laptop and wrong on the
internet — `deploy.ps1` refuses `-AllowUnauthenticated` with an empty
allowlist. Enforcement lives in `open_connector`, so every caller (CLI, demo
server, agent tool) passes through it; `tests/test_source_allowlist.py` covers
the bypasses that matter, including `..` escapes and `gs://data` wrongly
admitting `gs://data-prod`.

Two things a public deployment still does **not** have: rate limiting (a bored
visitor can burn Vertex tokens — `--max-instances 3` caps the blast radius but
does not stop it), and any audit of who scanned what. If either matters, put
IAP in front instead of going fully public.

### Access and cost

Private by default (`--no-allow-unauthenticated`), reached through
`proxy.ps1`, which attaches your identity to each request — a browser alone
sends no token, so the URL is useless to anyone else.

`-AllowUnauthenticated` makes it public. Think hard first: anyone with the URL
could scan your buckets and spend your Vertex and DLP budget.

Scales to zero, so idle cost is nothing but image storage. The tradeoff is a
~20s cold start on the first click, which looks broken in a demo. Either warm
it with a scan a minute beforehand, or deploy with `-MinInstances 1` (a few
dollars a month) for a client-facing day.

### Other entry points

```bash
adk web                  # ADK's own chat UI; pick "estate_profiler"
adk run estate_profiler  # terminal chat

# No agent at all — just the profiler
python -m estate_profiler.cli sample_estate --report --dashboard
python -m estate_profiler.cli gs://client-estate --max-files 5000 --dlp --bigquery
```

### If the agent says it is offline

Vertex AI needs Application Default Credentials:

```bash
gcloud auth application-default login
```

Or skip GCP auth entirely with an AI Studio key — useful on a laptop that
isn't signed into the client's project:

```bash
# in .env
GOOGLE_GENAI_USE_VERTEXAI=FALSE
GOOGLE_API_KEY=your-key
```

## Configuration

Copy `.env.example` to `.env` (ADK loads it automatically) and set what you need:

| Variable | Default | Purpose |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | — | Required for Cloud DLP and BigQuery |
| `GOOGLE_CLOUD_LOCATION` | `us-central1` | Vertex AI region |
| `GOOGLE_GENAI_USE_VERTEXAI` | `TRUE` | Use Vertex AI rather than an API key |
| `PROFILER_MODEL` | `gemini-2.5-flash` | Model backing the agent |
| `PROFILER_MAX_FILES` | `25000` | Crawl ceiling |
| `PROFILER_SAMPLE_BYTES` | `1048576` | Bytes read per file |
| `PROFILER_USE_DLP` | `false` | Escalate the riskiest files to Cloud DLP |
| `PROFILER_BQ_DATASET` | `estate_profiler` | BigQuery dataset for the inventory |
| `PROFILER_RUN_ROOT` | `runs` | Where manifests and reports are written |

## Sources

| URI | Connector |
|---|---|
| `gs://bucket` or `gs://bucket/prefix` | Cloud Storage |
| `D:/shares/finance`, `/mnt/estate` | Local filesystem |
| `//fileserver/legal` | Mounted SMB / file share |

A mapped network drive looks like an ordinary directory tree to Python, so the
local connector covers the file-share case without separate credentials.
SharePoint/OneDrive and S3/Azure Blob are deliberately out of scope for v1 —
`open_connector` rejects them with a clear message rather than failing obscurely.

## Outputs

Each run writes to `runs/<run_id>/`:

- `manifest.jsonl` — one row per file, every signal, the queryable asset
- `summary.json` — the rollups
- `report.md` — a client-ready brief with a prioritised action sequence
- `dashboard.html` — a self-contained interactive dashboard (no network needed)

Plus, on request, a partitioned and clustered **BigQuery** table so a large
inventory stays queryable:

```sql
SELECT disposition, sensitivity_level, COUNT(*) AS files,
       ROUND(SUM(size_bytes)/POW(1024,3), 2) AS gb
FROM `project.estate_profiler.file_inventory`
WHERE run_id = '...'
GROUP BY 1, 2 ORDER BY files DESC
```

## How the scoring works

`readiness_score` is a weighted blend of five dimensions, each 0–100:

| Dimension | Weight | Asks |
|---|---|---|
| `extractability` | 0.32 | Can text be obtained, and at what cost? |
| `substance` | 0.22 | Is there enough real text to be worth retrieving? |
| `chunkability` | 0.14 | Does it split into sensible retrieval units? |
| `cleanliness` | 0.16 | Is it a unique document or the fourth copy of one? |
| `governance` | 0.16 | Can it be indexed without creating an exposure problem? |

Weights live in `estate_profiler/profiling/readiness.py` and are meant to be
retuned per engagement — a regulated client weights `governance` far higher
than a general knowledge-base build.

`disposition` is a decision, not a score, and overrides the blend: live
credentials always quarantine, exact duplicates always exclude.

## Accuracy and honesty

- **Local sensitivity detection is indicative, not adjudicated.** Patterns are
  checksum-validated where a checksum exists (Luhn, IBAN mod-97, ABA, Aadhaar
  Verhoeff) and context-boosted elsewhere, so the *ranking* is trustworthy even
  though individual hits are not. Set `PROFILER_USE_DLP=true` to escalate the
  top-ranked files to Cloud DLP for auditable findings.
- **Scans sample.** `sample_bytes` bounds the read per file. Files larger than
  `full_hash_max_bytes` are hashed on their sample, which still reliably catches
  identical copies.
- **The agent never echoes sensitive values.** It reports what *type* of
  sensitive data was found and where, never the matched string.
- **Truncated scans say so.** If a crawl hits `max_files`, every report states
  that the figures describe the scanned subset.

## Testing

One command:

```bash
python run_tests.py            # offline only — no credentials, no network, no cost
python run_tests.py --live     # + a real Vertex agent turn (spends tokens)
python run_tests.py --deployed # + the hosted Cloud Run service
python run_tests.py --all      # everything
```

It prints a per-suite pass/fail summary and says *why* anything was skipped,
rather than quietly passing. The live and deployed checks are opt-in because
they cost money.

The suites can also be run individually:

```bash
python tests/test_profiling.py      # 39 - every profiling pass in isolation
python tests/test_end_to_end.py     # 25 - full scan, outputs, ADK tool layer
python tests/test_demo_server.py    # 12 - starts the demo server, drives the API
python tests/smoke_live_agent.py    # real Vertex AI turns, real tool dispatch
python deploy/verify_deployment.py  # drives the deployed Cloud Run service
```

### What each one covers

| Suite | Covers |
|---|---|
| `test_profiling` | Magic-byte sniffing, text extraction, every PII checksum validator, dedupe precision *and* recall, health rules, disposition logic |
| `test_end_to_end` | Builds the synthetic estate and asserts every planted condition is found; manifest integrity; BigQuery schema conformance; that the tool layer never returns bulk rows or sensitive values |
| `test_demo_server` | Boots the real server on a free port: health, static assets, scan streaming, all four downloads, 404s, 400s, that DLP stays opt-in, and that the served dashboard leaks no sensitive values |
| `smoke_live_agent` | The only thing the others can't prove — that the model actually answers, dispatches the right tools, and never echoes a matched secret. **Run this before a client demo.** |

The synthetic estate is what makes this testable: it plants encrypted and
scanned PDFs, a spreadsheet renamed `.pdf`, legacy Office binaries, zero-byte
files, exact and near duplicates, and syntactically valid (but fake) credentials
— then every suite asserts each one is detected.

### Before a demo

```bash
python tests/smoke_live_agent.py    # fails loudly, with the fix, if auth lapsed
```

ADC tokens expire. This is the check that catches it before a client does.

## Layout

```
Dockerfile               container for Cloud Run (bakes in the sample estate)
deploy/
  deploy.ps1             idempotent Cloud Run deploy + service account + IAM
  proxy.ps1              authenticated tunnel to the private service
  expose_public.ps1      external HTTPS load balancer (nip.io managed cert)
  wait_for_cert.ps1      poll until the managed certificate is ACTIVE
  teardown_public.ps1    remove the public endpoint again
  verify_deployment.py   drives the deployed service end to end
demo/
  server.py              FastAPI demo console — scan streaming + agent chat
  static/                the console UI (no framework, no build step)
                         honours PORT / HOST, so it runs under Cloud Run as-is
estate_profiler/
  agent.py               root_agent — the ADK entry point
  tools.py               ADK tool surface (returns summaries, never bulk rows)
  engine.py              scan orchestration and rollups
  cli.py                 run the profiler without the agent
  config.py              settings
  connectors/            local / SMB and GCS sources
  profiling/
    sniff.py             magic-byte content detection
    text_extract.py      stdlib-only text extraction (OOXML, PDF, HTML, RTF)
    sensitivity.py       PII/PHI/credential detection + DLP escalation
    dedupe.py            exact hashing + two-stage near-duplicate detection
    format_health.py     issue detection and remediation advice
    readiness.py         the scoring model and disposition rules
  outputs/               Markdown + JSON report, BigQuery loader, HTML dashboard
tools/make_sample_estate.py   synthetic estate generator
tests/                   unit + end-to-end suites
```
