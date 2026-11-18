"""ADK tools: a thin, model-facing wrapper over the profiling engine.

Design rule for every tool here: return *summaries and decisions*, never bulk
data. The full manifest stays on disk — a 50k-file estate would otherwise flood
the model's context with rows it cannot usefully reason over anyway.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from google.adk.tools import ToolContext

from .config import SETTINGS
from .engine import RunStore, latest_run_id, scan_estate
from .outputs import bq as bq_output
from .outputs import dashboard as dashboard_output
from .outputs import report as report_output

_RUN_KEY = "estate_run_id"


def _resolve_run_id(tool_context: ToolContext, run_id: str = "") -> str | None:
    """Prefer an explicit id, then this session's last scan, then the newest on disk."""
    if run_id:
        return run_id
    from_state = tool_context.state.get(_RUN_KEY)
    if from_state:
        return from_state
    return latest_run_id(SETTINGS)


def _err(message: str, **extra: Any) -> dict:
    return {"status": "error", "error": message, **extra}


def scan_unstructured_estate(
    source_uri: str,
    max_files: int,
    sample_bytes: int,
    use_dlp: bool,
    tool_context: ToolContext,
) -> dict:
    """Crawl an unstructured estate and profile every file it finds.

    Runs the full triage in one pass: inventory, magic-byte format health,
    text extraction, sensitivity detection, exact and near-duplicate detection,
    and Document AI / RAG readiness scoring.

    Args:
        source_uri: Where the estate lives. Either a GCS URI (``gs://bucket`` or
            ``gs://bucket/prefix``) or a local / mounted-share path such as
            ``D:/shares/finance`` or ``//fileserver/legal``.
        max_files: Ceiling on files to crawl. Pass 0 to use the configured
            default (25,000). Lower it for a fast first look at a huge estate.
        sample_bytes: Bytes read per file for hashing, text extraction and PII
            detection. Pass 0 for the default (1 MB). Larger means better
            near-duplicate and PII recall at the cost of scan time.
        use_dlp: Escalate the highest-risk files to Cloud DLP. **Pass false
            unless the user explicitly asked for DLP**: it is a billed API and
            turning it on is the user's cost decision, not yours. Local
            detection runs either way, so false is always a valid scan.

    Returns:
        A dict with the run_id and headline figures for every triage dimension.
        The per-file manifest is written to disk, not returned.
    """
    try:
        summary = scan_estate(
            source_uri,
            settings=SETTINGS,
            max_files=max_files or None,
            sample_bytes=sample_bytes or None,
            use_dlp=use_dlp,
        )
    except (FileNotFoundError, NotADirectoryError, ValueError, PermissionError) as exc:
        # PermissionError is the allowlist refusing a source; its message names
        # what is permitted, so the agent can relay that rather than retrying.
        return _err(str(exc), source_uri=source_uri)
    except Exception as exc:  # noqa: BLE001 - surface the cause to the model
        return _err(f"{type(exc).__name__}: {exc}", source_uri=source_uri)

    tool_context.state[_RUN_KEY] = summary["run_id"]
    inv, dup, sens = summary["inventory"], summary["duplication"], summary["readiness"]
    return {
        "status": "ok",
        "run_id": summary["run_id"],
        "source_uri": source_uri,
        "scan": summary["scan"],
        "headline": {
            "total_files": inv["total_files"],
            "total_gb": inv["total_gb"],
            "distinct_extensions": inv["distinct_extensions"],
            "duplicate_files": dup["duplicate_files"],
            "reclaimable_gb": dup["redundant_gb"],
            "reclaimable_pct": dup["reclaimable_pct"],
            "high_or_critical_sensitivity": summary["sensitivity"]["high_or_critical"],
            "credential_exposures": summary["sensitivity"]["credential_exposures"],
            "healthy_pct": summary["format_health"]["processable_pct"],
            "mean_readiness": sens["mean_score"],
            "ingest_now_pct": sens["ingest_now_pct"],
            "needs_document_ai": sens["needs_document_ai"],
            "est_total_chunks": sens["est_total_chunks"],
        },
        "by_disposition": sens["by_disposition"],
        "next": "Call get_estate_findings for any dimension's detail, or generate_estate_report to produce deliverables.",
    }


def get_estate_findings(dimension: str, run_id: str, tool_context: ToolContext) -> dict:
    """Return the detailed findings for one triage dimension of a completed scan.

    Args:
        dimension: One of ``inventory``, ``duplication``, ``sensitivity``,
            ``format_health``, ``readiness``, or ``all``.
        run_id: The scan to read. Pass an empty string for the most recent run.

    Returns:
        The requested slice of the run summary.
    """
    resolved = _resolve_run_id(tool_context, run_id)
    if not resolved:
        return _err("No scan has been run yet. Call scan_unstructured_estate first.")

    summary = RunStore(resolved, SETTINGS).read_summary()
    if not summary:
        return _err(f"No summary found for run {resolved}.")

    valid = {"inventory", "duplication", "sensitivity", "format_health", "readiness"}
    if dimension == "all":
        return {"status": "ok", "run_id": resolved, **{k: summary[k] for k in valid}}
    if dimension not in valid:
        return _err(f"Unknown dimension '{dimension}'. Valid: {sorted(valid)} or 'all'.")
    return {"status": "ok", "run_id": resolved, "dimension": dimension, dimension: summary[dimension]}


def query_estate_files(
    run_id: str,
    disposition: str,
    sensitivity_level: str,
    extension: str,
    path_contains: str,
    limit: int,
    tool_context: ToolContext,
) -> dict:
    """Filter the per-file manifest and return a small sample of matching files.

    Use this to answer specific questions ("which files are quarantined?",
    "what is in /Finance that needs OCR?") without loading the whole manifest.

    Args:
        run_id: The scan to query. Empty string uses the most recent run.
        disposition: Filter by disposition (``ingest_now``,
            ``ingest_after_processing``, ``remediate``, ``quarantine``,
            ``exclude``). Empty string means no filter.
        sensitivity_level: Filter by ``none``/``low``/``medium``/``high``/
            ``critical``. Empty string means no filter.
        extension: Filter by file extension without the dot. Empty for no filter.
        path_contains: Case-insensitive substring match on the relative path.
            Empty for no filter.
        limit: Maximum files to return. Pass 0 for 25. Capped at 100.

    Returns:
        The match count plus up to ``limit`` file records.
    """
    resolved = _resolve_run_id(tool_context, run_id)
    if not resolved:
        return _err("No scan has been run yet. Call scan_unstructured_estate first.")

    rows = RunStore(resolved, SETTINGS).read_manifest()
    if not rows:
        return _err(f"No manifest found for run {resolved}.")

    needle = path_contains.lower()
    matches = [
        r for r in rows
        if (not disposition or r.get("disposition") == disposition)
        and (not sensitivity_level or r.get("sensitivity_level") == sensitivity_level)
        and (not extension or r.get("ext") == extension.lstrip(".").lower())
        and (not needle or needle in r.get("rel_path", "").lower())
    ]
    matches.sort(key=lambda r: (-(r.get("sensitivity_score") or 0), -(r.get("size_bytes") or 0)))

    capped = max(1, min(limit or 25, 100))
    fields = (
        "rel_path", "ext", "size_bytes", "modified", "sniffed_type", "dup_status",
        "health_status", "health_issues", "sensitivity_level", "sensitivity_score",
        "sensitivity_categories", "readiness_score", "disposition",
        "recommended_processor", "blocking_reason",
    )
    return {
        "status": "ok",
        "run_id": resolved,
        "match_count": len(matches),
        "returned": min(len(matches), capped),
        "files": [{k: r.get(k) for k in fields} for r in matches[:capped]],
    }


def generate_estate_report(run_id: str, output_dir: str, tool_context: ToolContext) -> dict:
    """Write the client deliverables for a scan: a Markdown report and JSON summary.

    Args:
        run_id: The scan to report on. Empty string uses the most recent run.
        output_dir: Directory to write into. Empty string writes into the run's
            own directory alongside the manifest.

    Returns:
        Paths to the written files and the report's headline numbers.
    """
    resolved = _resolve_run_id(tool_context, run_id)
    if not resolved:
        return _err("No scan has been run yet. Call scan_unstructured_estate first.")

    store = RunStore(resolved, SETTINGS)
    summary = store.read_summary()
    if not summary:
        return _err(f"No summary found for run {resolved}.")

    target = Path(output_dir).resolve() if output_dir else store.dir
    try:
        paths = report_output.write_reports(summary, target)
    except OSError as exc:
        return _err(f"Could not write report: {exc}", output_dir=str(target))

    return {
        "status": "ok",
        "run_id": resolved,
        "markdown_path": paths["markdown"],
        "json_path": paths["json"],
        "manifest_path": str(store.manifest_path),
        "recommended_steps": report_output.next_steps(summary, plain=True),
    }


def build_estate_dashboard(run_id: str, output_path: str, tool_context: ToolContext) -> dict:
    """Build a self-contained interactive HTML dashboard for a scan.

    The page embeds the summary and renders its own charts, so it opens from
    disk or a bucket with nothing to install.

    Args:
        run_id: The scan to visualise. Empty string uses the most recent run.
        output_path: Where to write the .html file. Empty string writes
            ``dashboard.html`` into the run's directory.

    Returns:
        The path to the dashboard file.
    """
    resolved = _resolve_run_id(tool_context, run_id)
    if not resolved:
        return _err("No scan has been run yet. Call scan_unstructured_estate first.")

    store = RunStore(resolved, SETTINGS)
    summary = store.read_summary()
    if not summary:
        return _err(f"No summary found for run {resolved}.")

    target = Path(output_path).resolve() if output_path else store.dir / "dashboard.html"
    try:
        written = dashboard_output.write_dashboard(summary, target)
    except OSError as exc:
        return _err(f"Could not write dashboard: {exc}", output_path=str(target))

    return {
        "status": "ok",
        "run_id": resolved,
        "dashboard_path": written,
        "size_kb": round(Path(written).stat().st_size / 1024, 1),
        "note": "Self-contained HTML. Open it directly, or publish it for the client.",
    }


def load_inventory_to_bigquery(
    run_id: str,
    dataset: str,
    table: str,
    write_disposition: str,
    tool_context: ToolContext,
) -> dict:
    """Load the per-file manifest into BigQuery so the inventory stays queryable.

    Args:
        run_id: The scan to load. Empty string uses the most recent run.
        dataset: Target BigQuery dataset. Empty string uses the configured default.
        table: Target table. Empty string uses the configured default.
        write_disposition: ``WRITE_APPEND`` to add this run alongside previous
            ones (the default), or ``WRITE_TRUNCATE`` to replace the table.

    Returns:
        The table id, row count, and a starter query.
    """
    resolved = _resolve_run_id(tool_context, run_id)
    if not resolved:
        return _err("No scan has been run yet. Call scan_unstructured_estate first.")
    if not SETTINGS.project_id:
        return _err(
            "No GCP project configured. Set GOOGLE_CLOUD_PROJECT before loading to BigQuery."
        )

    store = RunStore(resolved, SETTINGS)
    if not store.manifest_path.exists():
        return _err(f"No manifest found for run {resolved}.")

    disposition = (write_disposition or "WRITE_APPEND").upper()
    if disposition not in ("WRITE_APPEND", "WRITE_TRUNCATE"):
        return _err("write_disposition must be WRITE_APPEND or WRITE_TRUNCATE.")

    try:
        result = bq_output.load_manifest(
            store.manifest_path,
            project_id=SETTINGS.project_id,
            dataset=dataset or SETTINGS.bq_dataset,
            table=table or SETTINGS.bq_table,
            location=SETTINGS.location if SETTINGS.location in ("US", "EU") else "US",
            write_disposition=disposition,
        )
    except Exception as exc:  # noqa: BLE001 - BigQuery raises a wide range
        return _err(f"BigQuery load failed: {type(exc).__name__}: {exc}")

    return {"status": "ok", "run_id": resolved, **result}


def list_estate_runs(tool_context: ToolContext) -> dict:
    """List completed scans on disk, newest first, with their headline figures."""
    root = SETTINGS.run_root
    if not root.exists():
        return {"status": "ok", "runs": [], "note": "No scans have been run yet."}

    runs = []
    for directory in sorted(root.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True):
        summary_file = directory / "summary.json"
        if not directory.is_dir() or not summary_file.exists():
            continue
        try:
            summary = json.loads(summary_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        runs.append(
            {
                "run_id": summary.get("run_id", directory.name),
                "source_uri": summary.get("source_uri"),
                "generated_at": summary.get("generated_at"),
                "total_files": summary.get("inventory", {}).get("total_files"),
                "total_gb": summary.get("inventory", {}).get("total_gb"),
                "mean_readiness": summary.get("readiness", {}).get("mean_score"),
            }
        )
        if len(runs) >= 20:
            break
    return {"status": "ok", "runs": runs}
