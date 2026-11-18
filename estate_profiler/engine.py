"""Scan orchestration: crawl an estate once, run every profiling pass, roll up.

The engine is deliberately free of ADK imports so it can be unit-tested, run
from a cron job, or driven by the agent — the tool layer in ``tools.py`` is a
thin wrapper over this.
"""

from __future__ import annotations

import json
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import SETTINGS, Settings
from .connectors import FileRef, open_connector
from .profiling import dedupe, format_health, readiness, sensitivity, sniff, text_extract

# Size buckets used in the estate profile, in bytes.
_SIZE_BUCKETS = (
    ("0-10 KB", 0, 10 * 1024),
    ("10-100 KB", 10 * 1024, 100 * 1024),
    ("100 KB-1 MB", 100 * 1024, 1024 * 1024),
    ("1-10 MB", 1024 * 1024, 10 * 1024 * 1024),
    ("10-100 MB", 10 * 1024 * 1024, 100 * 1024 * 1024),
    ("100 MB+", 100 * 1024 * 1024, float("inf")),
)

_AGE_BUCKETS = (
    ("< 1 year", 0, 365),
    ("1-3 years", 365, 1095),
    ("3-5 years", 1095, 1825),
    ("5-10 years", 1825, 3650),
    ("10+ years", 3650, float("inf")),
)


def _bucket(value: float, buckets) -> str:
    for label, low, high in buckets:
        if low <= value < high:
            return label
    return buckets[-1][0]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RunStore:
    """On-disk home for one scan's manifest and summary.

    The manifest stays on disk rather than in agent session state: an estate of
    100k files is far too large to carry in a model context, and the agent only
    ever needs the rollups.
    """

    def __init__(self, run_id: str, settings: Settings = SETTINGS):
        self.run_id = run_id
        self.dir = settings.run_dir(run_id)
        self.dir.mkdir(parents=True, exist_ok=True)

    @property
    def manifest_path(self) -> Path:
        return self.dir / "manifest.jsonl"

    @property
    def summary_path(self) -> Path:
        return self.dir / "summary.json"

    def write_manifest(self, rows: list[dict]) -> None:
        with open(self.manifest_path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def read_manifest(self) -> list[dict]:
        if not self.manifest_path.exists():
            return []
        with open(self.manifest_path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def write_summary(self, summary: dict) -> None:
        self.summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )

    def read_summary(self) -> dict:
        if not self.summary_path.exists():
            return {}
        return json.loads(self.summary_path.read_text(encoding="utf-8"))


def latest_run_id(settings: Settings = SETTINGS) -> str | None:
    root = settings.run_root
    if not root.exists():
        return None
    runs = [d for d in root.iterdir() if d.is_dir() and (d / "summary.json").exists()]
    if not runs:
        return None
    return max(runs, key=lambda d: d.stat().st_mtime).name


def scan_estate(
    source_uri: str,
    *,
    settings: Settings = SETTINGS,
    max_files: int | None = None,
    sample_bytes: int | None = None,
    use_dlp: bool | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> dict:
    """Crawl one source end to end and return the summary (also persisted to disk)."""
    started = time.monotonic()
    max_files = max_files or settings.max_files
    sample_bytes = sample_bytes or settings.sample_bytes
    use_dlp = settings.use_dlp if use_dlp is None else use_dlp

    connector = open_connector(source_uri, project=settings.project_id or None)
    run_id = f"{_now():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    store = RunStore(run_id, settings)

    now = _now()
    records: list[dict] = []
    sensitivity_by_uri: dict[str, sensitivity.SensitivityResult] = {}
    extraction_by_uri: dict[str, text_extract.Extraction] = {}
    health_by_uri: dict[str, format_health.HealthResult] = {}
    read_errors = 0

    # --- pass 1: per-file content profiling --------------------------------
    for index, ref in enumerate(connector.walk(max_files)):
        if progress and index % 250 == 0:
            progress(index, max_files)

        read_limit = min(sample_bytes, ref.size_bytes) if ref.size_bytes else 0
        data = connector.read(ref, read_limit) if read_limit else b""
        if read_limit and not data:
            read_errors += 1

        sniffed = sniff.sniff(data, ref.ext)
        extraction = text_extract.extract(data, sniffed, ref.ext)
        age_days = (now - ref.modified).days if ref.modified else None
        health = format_health.assess(
            ext=ref.ext, sniffed=sniffed, size_bytes=ref.size_bytes,
            extraction=extraction, age_days=age_days,
        )
        sens = sensitivity.analyse(extraction.text, ref.name)

        extraction_by_uri[ref.uri] = extraction
        health_by_uri[ref.uri] = health
        sensitivity_by_uri[ref.uri] = sens

        # Hash the full file when it is small enough, otherwise hash the sample.
        # A sample hash still catches identical copies of large files reliably,
        # which is what matters for dedupe savings.
        fully_hashed = ref.size_bytes <= settings.full_hash_max_bytes and len(data) >= ref.size_bytes
        digest = dedupe.content_hash(data) if data else None
        records.append(
            {
                "uri": ref.uri,
                "source": ref.source,
                "container": ref.container,
                "rel_path": ref.rel_path,
                "parent": ref.parent,
                "name": ref.name,
                "ext": ref.ext,
                "size_bytes": ref.size_bytes,
                "modified": ref.modified.isoformat() if ref.modified else None,
                "modified_ts": ref.modified.timestamp() if ref.modified else None,
                "age_days": age_days,
                "depth": ref.depth,
                "sniffed_type": sniffed,
                "extension_mismatch": "extension_mismatch" in health.issues,
                "content_hash": digest,
                "hash_scope": "full" if fully_hashed else "sample",
                "sketch": dedupe.sketch(extraction.text),
                "text_chars": extraction.char_count,
                "extract_method": extraction.method,
                "extractable": extraction.extractable,
                "needs_ocr": extraction.needs_ocr,
                "page_hint": extraction.page_hint,
                "health_status": health.status,
                "health_score": health.score,
                "health_issues": health.issues,
                "remediation": health.remediation,
                "sensitivity_score": sens.score,
                "sensitivity_level": sens.level,
                "sensitivity_categories": sens.categories,
                "sensitivity_findings": sens.findings,
                "content_classes": sens.content_classes,
                "dlp_checked": False,
                "dlp_infotypes": {},
            }
        )

    # --- pass 2: optional DLP escalation on the riskiest files --------------
    if use_dlp and settings.project_id:
        ranked = sorted(
            (r for r in records if r["text_chars"] > 0),
            key=lambda r: r["sensitivity_score"],
            reverse=True,
        )[: settings.dlp_sample_limit]
        for record in ranked:
            text = extraction_by_uri[record["uri"]].text
            counts = sensitivity.inspect_with_dlp(text, settings.project_id)
            merged = sensitivity.merge_dlp(sensitivity_by_uri[record["uri"]], counts)
            record.update(
                {
                    "sensitivity_score": merged.score,
                    "sensitivity_level": merged.level,
                    "sensitivity_categories": merged.categories,
                    "dlp_checked": True,
                    "dlp_infotypes": merged.dlp_infotypes,
                }
            )

    # --- pass 3: dedupe across the whole estate -----------------------------
    dup_report = dedupe.build_report(records)

    # --- pass 4: readiness, which depends on dedupe status ------------------
    for record in records:
        status = dup_report.status_by_uri.get(record["uri"], "unique")
        ready = readiness.score(
            extraction=extraction_by_uri[record["uri"]],
            health=health_by_uri[record["uri"]],
            sensitivity=sensitivity_by_uri[record["uri"]],
            dup_status=status,
            sniffed=record["sniffed_type"],
            ext=record["ext"],
            name=record["name"],
        )
        record.update(
            {
                "dup_status": status,
                "readiness_score": ready.score,
                "readiness_band": ready.band,
                "disposition": ready.disposition,
                "readiness_dimensions": ready.dimensions,
                "recommended_processor": ready.recommended_processor,
                "est_tokens": ready.est_tokens,
                "est_chunks": ready.est_chunks,
                "blocking_reason": ready.blocking_reason,
                "rationale": ready.rationale,
                "run_id": run_id,
            }
        )
        # The full sketch is working memory for the dedupe pass, not something
        # to persist per file; keep a short digest so a row stays identifiable.
        record["text_sketch"] = dedupe.sketch_digest(record.pop("sketch"))

    store.write_manifest(records)
    summary = _summarise(
        run_id=run_id,
        source_uri=source_uri,
        container=connector.container,
        records=records,
        dup_report=dup_report,
        elapsed_s=round(time.monotonic() - started, 2),
        read_errors=read_errors,
        max_files=max_files,
        sample_bytes=sample_bytes,
        dlp_used=bool(use_dlp and settings.project_id),
    )
    store.write_summary(summary)
    return summary


def _top(counter: Counter, n: int = 15) -> list[dict]:
    return [{"key": k, "count": v} for k, v in counter.most_common(n)]


def _summarise(
    *,
    run_id: str,
    source_uri: str,
    container: str,
    records: list[dict],
    dup_report: dedupe.DedupeReport,
    elapsed_s: float,
    read_errors: int,
    max_files: int,
    sample_bytes: int,
    dlp_used: bool,
) -> dict:
    total_files = len(records)
    total_bytes = sum(r["size_bytes"] for r in records)

    by_ext = Counter(r["ext"] or "(none)" for r in records)
    bytes_by_ext: dict[str, int] = defaultdict(int)
    for record in records:
        bytes_by_ext[record["ext"] or "(none)"] += record["size_bytes"]

    by_sniff = Counter(r["sniffed_type"] for r in records)
    by_health = Counter(r["health_status"] for r in records)
    by_sensitivity = Counter(r["sensitivity_level"] for r in records)
    by_band = Counter(r["readiness_band"] for r in records)
    by_disposition = Counter(r["disposition"] for r in records)
    by_processor = Counter(r["recommended_processor"] for r in records)
    by_size = Counter(_bucket(r["size_bytes"], _SIZE_BUCKETS) for r in records)
    by_age = Counter(
        _bucket(r["age_days"], _AGE_BUCKETS) for r in records if r["age_days"] is not None
    )

    issue_counts: Counter = Counter()
    for record in records:
        issue_counts.update(record["health_issues"])

    finding_counts: Counter = Counter()
    for record in records:
        finding_counts.update(record["sensitivity_findings"].keys())

    class_counts: Counter = Counter()
    for record in records:
        class_counts.update(record["content_classes"])

    # Largest folders by byte volume: where a migration's effort actually goes.
    folder_bytes: dict[str, int] = defaultdict(int)
    folder_files: Counter = Counter()
    folder_risk: dict[str, int] = defaultdict(int)
    for record in records:
        folder_bytes[record["parent"]] += record["size_bytes"]
        folder_files[record["parent"]] += 1
        folder_risk[record["parent"]] = max(folder_risk[record["parent"]], record["sensitivity_score"])
    hotspots = sorted(folder_bytes.items(), key=lambda kv: kv[1], reverse=True)[:15]

    ready_now = by_disposition.get("ingest_now", 0)
    after_processing = by_disposition.get("ingest_after_processing", 0)
    scores = [r["readiness_score"] for r in records] or [0]
    sensitive_files = [r for r in records if r["sensitivity_level"] in ("high", "critical")]

    return {
        "run_id": run_id,
        "generated_at": _now().isoformat(),
        "source_uri": source_uri,
        "container": container,
        "scan": {
            "elapsed_seconds": elapsed_s,
            "max_files": max_files,
            "sample_bytes": sample_bytes,
            "read_errors": read_errors,
            "truncated": total_files >= max_files,
            "dlp_used": dlp_used,
        },
        "inventory": {
            "total_files": total_files,
            "total_bytes": total_bytes,
            "total_gb": round(total_bytes / 1024**3, 3),
            "distinct_extensions": len(by_ext),
            "max_depth": max((r["depth"] for r in records), default=0),
            "by_extension": _top(by_ext),
            "bytes_by_extension": _top(Counter(bytes_by_ext)),
            "by_sniffed_type": _top(by_sniff),
            "by_size_bucket": [{"key": k, "count": by_size.get(k, 0)} for k, _, _ in _SIZE_BUCKETS],
            "by_age_bucket": [{"key": k, "count": by_age.get(k, 0)} for k, _, _ in _AGE_BUCKETS],
            "folder_hotspots": [
                {
                    "folder": folder,
                    "bytes": size,
                    "files": folder_files[folder],
                    "max_sensitivity": folder_risk[folder],
                }
                for folder, size in hotspots
            ],
        },
        "duplication": {
            "exact_groups": len(dup_report.exact_groups),
            "near_groups": len(dup_report.near_groups),
            "duplicate_files": dup_report.duplicate_file_count,
            "duplicate_pct": round(100 * dup_report.duplicate_file_count / total_files, 1) if total_files else 0.0,
            "redundant_bytes": dup_report.total_redundant_bytes,
            "redundant_gb": round(dup_report.total_redundant_bytes / 1024**3, 3),
            "reclaimable_pct": round(100 * dup_report.total_redundant_bytes / total_bytes, 1) if total_bytes else 0.0,
            "top_exact_groups": [
                {"key": g.key, "members": len(g.members), "redundant_bytes": g.redundant_bytes,
                 "keeper": g.keeper, "examples": g.members[1:4]}
                for g in dup_report.exact_groups[:10]
            ],
            "top_near_groups": [
                {"key": g.key, "members": len(g.members), "redundant_bytes": g.redundant_bytes,
                 "similarity": g.similarity, "keeper": g.keeper, "examples": g.members[1:4]}
                for g in dup_report.near_groups[:10]
            ],
        },
        "sensitivity": {
            "by_level": [{"key": k, "count": by_sensitivity.get(k, 0)}
                         for k in ("none", "low", "medium", "high", "critical")],
            "high_or_critical": len(sensitive_files),
            "high_or_critical_pct": round(100 * len(sensitive_files) / total_files, 1) if total_files else 0.0,
            "credential_exposures": sum(1 for r in records if "credential" in r["sensitivity_categories"]),
            "by_detector": _top(finding_counts, 20),
            "content_classes": _top(class_counts, 10),
            "top_files": [
                {"uri": r["uri"], "rel_path": r["rel_path"], "score": r["sensitivity_score"],
                 "level": r["sensitivity_level"], "categories": r["sensitivity_categories"],
                 "findings": r["sensitivity_findings"]}
                for r in sorted(records, key=lambda r: r["sensitivity_score"], reverse=True)[:25]
                if r["sensitivity_score"] > 0
            ],
        },
        "format_health": {
            "by_status": [{"key": k, "count": by_health.get(k, 0)} for k in ("healthy", "degraded", "blocked")],
            "by_issue": _top(issue_counts, 20),
            "processable_pct": round(
                100 * sum(1 for r in records if r["health_status"] == "healthy") / total_files, 1
            ) if total_files else 0.0,
        },
        "readiness": {
            "mean_score": round(sum(scores) / len(scores), 1),
            "median_score": sorted(scores)[len(scores) // 2],
            "by_band": [{"key": k, "count": by_band.get(k, 0)}
                        for k in ("ready", "conditional", "needs_work", "not_ready")],
            "by_disposition": [{"key": k, "count": by_disposition.get(k, 0)} for k in readiness.DISPOSITIONS],
            "by_processor": _top(by_processor, 10),
            "ingest_now_pct": round(100 * ready_now / total_files, 1) if total_files else 0.0,
            "needs_document_ai": sum(
                v for k, v in by_processor.items() if k != readiness.PROCESSOR_NONE
            ),
            "est_total_tokens": sum(r["est_tokens"] for r in records),
            "est_total_chunks": sum(r["est_chunks"] for r in records),
            "corpus_after_triage": ready_now + after_processing,
        },
    }
