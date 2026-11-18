"""Run the profiler without the agent.

Useful for scheduled scans, CI, and for profiling an estate too large to sit
through interactively:

    python -m estate_profiler.cli gs://client-estate --max-files 5000 --report
"""

from __future__ import annotations

import argparse
import sys

from .config import SETTINGS
from .engine import RunStore, scan_estate
from .outputs import dashboard as dashboard_output
from .outputs import report as report_output


def _progress(seen: int, limit: int) -> None:
    if seen:
        print(f"  ... {seen:,} files", end="\r", file=sys.stderr, flush=True)


def _size(num_bytes: int) -> str:
    for unit, scale in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if num_bytes >= scale:
            return f"{num_bytes / scale:.1f} {unit}"
    return f"{num_bytes} B"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="estate_profiler",
        description="Triage an unstructured data estate for migration and RAG readiness.",
    )
    parser.add_argument("source", help="gs://bucket[/prefix], a local path, or a mounted share")
    parser.add_argument("--max-files", type=int, default=0, help="crawl ceiling (default: configured)")
    parser.add_argument("--sample-bytes", type=int, default=0, help="bytes read per file")
    parser.add_argument("--dlp", action="store_true", help="escalate the riskiest files to Cloud DLP")
    parser.add_argument("--report", action="store_true", help="write report.md and summary.json")
    parser.add_argument("--dashboard", action="store_true", help="write dashboard.html")
    parser.add_argument("--bigquery", action="store_true", help="load the manifest into BigQuery")
    args = parser.parse_args(argv)

    print(f"Scanning {args.source} ...", file=sys.stderr)
    try:
        summary = scan_estate(
            args.source,
            settings=SETTINGS,
            max_files=args.max_files or None,
            sample_bytes=args.sample_bytes or None,
            use_dlp=args.dlp,
            progress=_progress,
        )
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    store = RunStore(summary["run_id"], SETTINGS)
    inv, dup, ready = summary["inventory"], summary["duplication"], summary["readiness"]
    sens = summary["sensitivity"]

    print(f"\nRun {summary['run_id']}  ({summary['scan']['elapsed_seconds']}s)")
    print(f"  {inv['total_files']:,} files · {_size(inv['total_bytes'])} · {inv['distinct_extensions']} types")
    print(f"  ingest now:      {ready['ingest_now_pct']}%  (mean readiness {ready['mean_score']}/100)")
    print(f"  needs Doc AI:    {ready['needs_document_ai']:,} files")
    print(f"  duplicates:      {dup['duplicate_files']:,} files · "
          f"{_size(dup['redundant_bytes'])} reclaimable")
    print(f"  sensitive:       {sens['high_or_critical']:,} high/critical · "
          f"{sens['credential_exposures']:,} credential exposures")
    print(f"  manifest:        {store.manifest_path}")

    if args.report:
        paths = report_output.write_reports(summary, store.dir)
        print(f"  report:          {paths['markdown']}")
    if args.dashboard:
        print(f"  dashboard:       {dashboard_output.write_dashboard(summary, store.dir / 'dashboard.html')}")
    if args.bigquery:
        if not SETTINGS.project_id:
            print("  bigquery:        skipped (set GOOGLE_CLOUD_PROJECT)", file=sys.stderr)
        else:
            from .outputs import bq

            result = bq.load_manifest(
                store.manifest_path,
                project_id=SETTINGS.project_id,
                dataset=SETTINGS.bq_dataset,
                table=SETTINGS.bq_table,
            )
            print(f"  bigquery:        {result.get('table')} ({result.get('rows', 0):,} rows)")

    if sens["credential_exposures"]:
        sys.stdout.flush()
        print(f"\nWARNING: {sens['credential_exposures']:,} files contain credential patterns. "
              "Treat as a security incident, not a migration task.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
