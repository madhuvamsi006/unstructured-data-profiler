"""Client-facing report generation: JSON manifest summary plus a Markdown brief."""

from __future__ import annotations

from pathlib import Path


def _gb(num_bytes: int) -> str:
    if num_bytes >= 1024**3:
        return f"{num_bytes / 1024**3:.2f} GB"
    if num_bytes >= 1024**2:
        return f"{num_bytes / 1024**2:.1f} MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes} B"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_None._\n"
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(out) + "\n"


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "0.0%"


def _lookup(items: list[dict], key: str) -> int:
    for item in items:
        if item["key"] == key:
            return item["count"]
    return 0


def build_markdown(summary: dict) -> str:
    """Render the summary as a brief a client can read without further explanation."""
    inv = summary["inventory"]
    dup = summary["duplication"]
    sens = summary["sensitivity"]
    health = summary["format_health"]
    ready = summary["readiness"]
    total = inv["total_files"]

    ingest_now = _lookup(ready["by_disposition"], "ingest_now")
    after_processing = _lookup(ready["by_disposition"], "ingest_after_processing")
    remediate = _lookup(ready["by_disposition"], "remediate")
    quarantine = _lookup(ready["by_disposition"], "quarantine")
    exclude = _lookup(ready["by_disposition"], "exclude")

    lines: list[str] = [
        "# Unstructured Estate Triage Report",
        "",
        f"**Source:** `{summary['source_uri']}`  ",
        f"**Run:** `{summary['run_id']}`  ",
        f"**Generated:** {summary['generated_at']}  ",
        f"**Scan time:** {summary['scan']['elapsed_seconds']}s"
        + ("  ·  **DLP:** enabled" if summary["scan"]["dlp_used"] else "  ·  **DLP:** local heuristics only"),
        "",
    ]

    if summary["scan"]["truncated"]:
        lines += [
            f"> **Note:** the crawl hit its `max_files` ceiling of {summary['scan']['max_files']:,}. "
            "Figures below describe the scanned subset, not the whole estate.",
            "",
        ]

    lines += [
        "## Executive summary",
        "",
        f"The scan covered **{total:,} files** totalling **{_gb(inv['total_bytes'])}** across "
        f"{inv['distinct_extensions']} distinct file types.",
        "",
        f"- **{_pct(ingest_now, total)} ({ingest_now:,} files) can be ingested today** with no preprocessing.",
        f"- **{_pct(after_processing, total)} ({after_processing:,} files) need a Document AI processor first** "
        f"— {ready['needs_document_ai']:,} files have a recommended processor attached.",
        f"- **{_pct(dup['duplicate_files'], total)} of files are duplicates**, representing "
        f"**{_gb(dup['redundant_bytes'])} ({dup['reclaimable_pct']}%) of reclaimable storage**.",
        f"- **{sens['high_or_critical']:,} files carry high or critical sensitivity** "
        f"({sens['high_or_critical_pct']}%), including **{sens['credential_exposures']:,} with live credential "
        "patterns** that must be remediated before any indexing.",
        f"- Mean RAG-readiness score across the estate is **{ready['mean_score']}/100** "
        f"(median {ready['median_score']}).",
        "",
        f"After triage, the indexable corpus is approximately **{ready['corpus_after_triage']:,} documents** "
        f"→ **~{ready['est_total_chunks']:,} retrieval chunks** / **~{ready['est_total_tokens']:,} tokens**.",
        "",
        "## Disposition",
        "",
        _table(
            ["Disposition", "Files", "Share", "Meaning"],
            [
                ["`ingest_now`", f"{ingest_now:,}", _pct(ingest_now, total), "Ready for the pipeline as-is"],
                ["`ingest_after_processing`", f"{after_processing:,}", _pct(after_processing, total),
                 "Needs OCR, conversion, or archive expansion"],
                ["`remediate`", f"{remediate:,}", _pct(remediate, total), "Fix sensitivity or format before ingest"],
                ["`quarantine`", f"{quarantine:,}", _pct(quarantine, total),
                 "Credentials, encrypted, or corrupt — hold out"],
                ["`exclude`", f"{exclude:,}", _pct(exclude, total), "Duplicates and non-documents"],
            ],
        ),
        "## Inventory",
        "",
        "### By file type",
        "",
        _table(
            ["Extension", "Files", "Share"],
            [[f"`{e['key']}`", f"{e['count']:,}", _pct(e["count"], total)] for e in inv["by_extension"][:12]],
        ),
        "### Detected content types (by magic bytes)",
        "",
        _table(
            ["Sniffed type", "Files"],
            [[f"`{e['key']}`", f"{e['count']:,}"] for e in inv["by_sniffed_type"][:12]],
        ),
        "### Size and age distribution",
        "",
        _table(["Size band", "Files"], [[b["key"], f"{b['count']:,}"] for b in inv["by_size_bucket"]]),
        "",
        _table(["Age band", "Files"], [[b["key"], f"{b['count']:,}"] for b in inv["by_age_bucket"]]),
        "### Largest folders",
        "",
        _table(
            ["Folder", "Files", "Size", "Max sensitivity"],
            [[f"`{h['folder']}`", f"{h['files']:,}", _gb(h["bytes"]), h["max_sensitivity"]]
             for h in inv["folder_hotspots"][:10]],
        ),
        "## Duplication",
        "",
        f"Found **{dup['exact_groups']:,} exact-duplicate groups** and **{dup['near_groups']:,} "
        f"near-duplicate clusters**, covering {dup['duplicate_files']:,} redundant files.",
        "",
        f"Reclaiming them recovers **{_gb(dup['redundant_bytes'])}** and, more importantly, stops the same "
        "answer being indexed several times — the single largest source of inconsistent RAG responses.",
        "",
        "### Largest exact-duplicate groups",
        "",
        _table(
            ["Copies", "Wasted", "Keeper"],
            [[g["members"], _gb(g["redundant_bytes"]), f"`{g['keeper'].rsplit('/', 1)[-1]}`"]
             for g in dup["top_exact_groups"][:8]],
        ),
        "### Largest near-duplicate clusters",
        "",
        _table(
            ["Files", "Similarity", "Wasted", "Keeper"],
            [[g["members"], f"{g['similarity']:.0%}", _gb(g["redundant_bytes"]),
              f"`{g['keeper'].rsplit('/', 1)[-1]}`"]
             for g in dup["top_near_groups"][:8]],
        ),
        "## Sensitivity",
        "",
        _table(
            ["Level", "Files", "Share"],
            [[b["key"], f"{b['count']:,}", _pct(b["count"], total)] for b in sens["by_level"]],
        ),
        "### What was detected",
        "",
        _table(
            ["Detector", "Files containing it"],
            [[f"`{d['key']}`", f"{d['count']:,}"] for d in sens["by_detector"][:15]],
        ),
    ]

    if sens["content_classes"]:
        lines += [
            "### Content classes",
            "",
            _table(["Class", "Files"], [[c["key"], f"{c['count']:,}"] for c in sens["content_classes"]]),
        ]

    if sens["top_files"]:
        lines += [
            "### Highest-risk files",
            "",
            _table(
                ["File", "Score", "Level", "Categories"],
                [[f"`{f['rel_path']}`", f["score"], f["level"], ", ".join(f["categories"]) or "—"]
                 for f in sens["top_files"][:12]],
            ),
            "> Findings are from pattern and checksum analysis"
            + (" confirmed against Cloud DLP." if summary["scan"]["dlp_used"]
               else ", not Cloud DLP. Re-run with DLP enabled for auditable, high-confidence findings."),
            "",
        ]

    lines += [
        "## Format health",
        "",
        f"**{health['processable_pct']}%** of files are healthy and directly processable.",
        "",
        _table(["Status", "Files"], [[b["key"], f"{b['count']:,}"] for b in health["by_status"]]),
        "### Issues found",
        "",
        _table(
            ["Issue", "Files"],
            [[f"`{i['key']}`", f"{i['count']:,}"] for i in health["by_issue"][:15]],
        ),
        "## Document AI / RAG readiness",
        "",
        _table(
            ["Band", "Files", "Share"],
            [[b["key"], f"{b['count']:,}", _pct(b["count"], total)] for b in ready["by_band"]],
        ),
        "### Recommended Document AI processors",
        "",
        _table(
            ["Processor", "Files"],
            [[f"`{p['key']}`", f"{p['count']:,}"] for p in ready["by_processor"]],
        ),
        "## Recommended next steps",
        "",
    ]

    lines += next_steps(summary)
    lines += [
        "",
        "---",
        "",
        f"_Generated by the Unstructured Data Profiler Agent. Per-file detail is in `manifest.jsonl` "
        f"({total:,} rows) alongside this report._",
    ]
    return "\n".join(lines) + "\n"


def next_steps(summary: dict, plain: bool = False) -> list[str]:
    """Order actions by what actually blocks the migration first.

    ``plain=True`` strips Markdown emphasis and numbering for the dashboard,
    which renders the same list as HTML.
    """
    steps = _next_steps(summary)
    if not plain:
        return steps
    import re as _re

    return [_re.sub(r"^\d+\.\s*", "", s).replace("**", "") for s in steps]


def _next_steps(summary: dict) -> list[str]:
    sens = summary["sensitivity"]
    dup = summary["duplication"]
    ready = summary["readiness"]
    health = summary["format_health"]
    steps: list[str] = []
    index = 1

    if sens["credential_exposures"]:
        steps.append(
            f"{index}. **Rotate and remove {sens['credential_exposures']:,} exposed credentials.** These files are "
            "quarantined from the corpus. Credential exposure is a live security incident, not a migration task — "
            "handle it before anything else."
        )
        index += 1

    if sens["high_or_critical"]:
        steps.append(
            f"{index}. **Decide the redaction policy for {sens['high_or_critical']:,} high/critical sensitivity files.** "
            "Options are DLP de-identification before indexing, ACL-mirrored indexing, or exclusion. This decision "
            "gates the governance design for the whole pipeline."
        )
        index += 1

    if dup["duplicate_files"]:
        steps.append(
            f"{index}. **Collapse {dup['duplicate_files']:,} duplicates to recover {_gb(dup['redundant_bytes'])}.** "
            "The manifest names a keeper per group, chosen by recency and path canonicality."
        )
        index += 1

    ocr_needed = sum(p["count"] for p in ready["by_processor"] if p["key"] in ("document-ocr", "form-parser"))
    if ocr_needed:
        steps.append(
            f"{index}. **Stand up a Document AI batch job for {ocr_needed:,} files with no text layer.** "
            "Scanned PDFs and images are invisible to any retrieval system until OCR gives them one."
        )
        index += 1

    legacy = _lookup(health["by_issue"], "legacy_format") + _lookup(health["by_issue"], "archive_unexpanded")
    if legacy:
        steps.append(
            f"{index}. **Convert or expand {legacy:,} legacy-format and archived files.** Legacy Office binaries "
            "and unexpanded archives are opaque to the pipeline and carry macro risk."
        )
        index += 1

    mismatch = _lookup(health["by_issue"], "extension_mismatch")
    if mismatch:
        steps.append(
            f"{index}. **Re-identify {mismatch:,} files whose extension disagrees with their content.** "
            "Routing on a wrong extension sends files to the wrong processor and fails silently."
        )
        index += 1

    steps.append(
        f"{index}. **Pilot the pipeline on the `ingest_now` cohort** "
        f"(~{ready['est_total_chunks']:,} chunks). It is clean, unique, and low-risk, so it validates the "
        "end-to-end design without waiting on remediation."
    )
    return steps


def write_reports(summary: dict, out_dir: Path) -> dict[str, str]:
    """Write report.md and summary.json into ``out_dir``; return the paths."""
    import json

    out_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = out_dir / "report.md"
    json_path = out_dir / "summary.json"
    markdown_path.write_text(build_markdown(summary), encoding="utf-8")
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {"markdown": str(markdown_path), "json": str(json_path)}
