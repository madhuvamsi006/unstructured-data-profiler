"""Format health: can this file be opened, trusted, and processed at all?

Health is separate from readiness. A file can be perfectly healthy and still be
a bad RAG candidate (a 4-byte "ok.txt"), and a file can be a superb RAG
candidate once a health problem is remediated (a scanned PDF after OCR).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .sniff import extension_matches
from .text_extract import Extraction

# Formats that no modern pipeline reads directly and that carry macro risk.
LEGACY_SNIFFS = frozenset({"ole2", "rtf", "postscript"})
ARCHIVE_SNIFFS = frozenset({"zip", "rar", "7z", "gzip", "zip-empty"})
MEDIA_SNIFFS = frozenset({"mp4", "mp3", "wav", "avi", "ogg", "flac", "matroska", "mpeg", "riff", "webp"})
IMAGE_SNIFFS = frozenset({"png", "jpeg", "gif", "bmp", "tiff", "webp"})
NON_DOCUMENT_SNIFFS = frozenset({"pe", "elf", "sqlite", "unknown-binary"})

# Issues ordered by how much they block a migration, worst first.
_SEVERITY = {
    "encrypted": 5,
    "corrupt": 5,
    "zero_byte": 4,
    "extension_mismatch": 4,
    "not_a_document": 3,
    "legacy_format": 3,
    "archive_unexpanded": 3,
    "needs_ocr": 2,
    "media_needs_transcription": 2,
    "oversized": 2,
    "no_extension": 1,
    "truncated_sample": 1,
    "stale": 1,
}

# Beyond this, a single file is an ingestion problem regardless of content.
_OVERSIZE_BYTES = 200 * 1024 * 1024
_STALE_DAYS = 365 * 5


@dataclass
class HealthResult:
    status: str = "healthy"                       # healthy | degraded | blocked
    issues: list[str] = field(default_factory=list)
    processable: bool = True                      # can a pipeline consume it today?
    remediation: str = ""                         # the single next action
    score: int = 100                              # 0-100 health score

    def add(self, issue: str) -> None:
        if issue not in self.issues:
            self.issues.append(issue)


_REMEDIATION = {
    "encrypted": "Obtain credentials or exclude; encrypted files cannot be indexed.",
    "corrupt": "Quarantine and attempt recovery from backup.",
    "zero_byte": "Delete; zero-byte files carry no content.",
    "extension_mismatch": "Re-identify by content and correct the extension before ingest.",
    "not_a_document": "Exclude from the document corpus; route to an application owner.",
    "legacy_format": "Convert to a modern format (OOXML/PDF) before ingest.",
    "archive_unexpanded": "Expand the archive and re-profile its contents.",
    "needs_ocr": "Route through Document AI OCR / Layout Parser to create a text layer.",
    "media_needs_transcription": "Route through Speech-to-Text if the audio is in scope.",
    "oversized": "Split before ingest; exceeds practical per-file processing limits.",
    "no_extension": "Set an extension from the sniffed type to keep downstream routing sane.",
    "truncated_sample": "Re-profile with a larger sample window for a definitive verdict.",
    "stale": "Confirm retention value with the business owner before migrating.",
}


def assess(
    *,
    ext: str,
    sniffed: str,
    size_bytes: int,
    extraction: Extraction,
    age_days: float | None = None,
) -> HealthResult:
    result = HealthResult()

    if size_bytes == 0 or sniffed == "empty":
        result.add("zero_byte")
        result.processable = False
    if sniffed == "zip-empty":
        result.add("zero_byte")
        result.processable = False

    if extraction.method in ("pdf-encrypted", "encrypted-ooxml") or sniffed == "ooxml-encrypted":
        result.add("encrypted")
        result.processable = False

    if extraction.method.endswith("-unreadable") or extraction.method == "pdf-no-recoverable-text":
        result.add("corrupt")
        result.processable = False

    if extraction.method.endswith("-truncated-sample"):
        result.add("truncated_sample")

    matches = extension_matches(ext, sniffed)
    if matches is False:
        result.add("extension_mismatch")
    if not ext and sniffed not in ("empty", "unknown-binary"):
        result.add("no_extension")

    if sniffed in LEGACY_SNIFFS:
        result.add("legacy_format")
        result.processable = False
    if sniffed in ARCHIVE_SNIFFS and sniffed != "zip-empty":
        result.add("archive_unexpanded")
        result.processable = False
    if sniffed in NON_DOCUMENT_SNIFFS:
        result.add("not_a_document")
        result.processable = False
    if sniffed in MEDIA_SNIFFS and sniffed not in IMAGE_SNIFFS:
        result.add("media_needs_transcription")
        result.processable = False

    if extraction.needs_ocr:
        result.add("needs_ocr")
        result.processable = False

    if size_bytes > _OVERSIZE_BYTES:
        result.add("oversized")

    if age_days is not None and age_days > _STALE_DAYS:
        result.add("stale")

    if not result.issues:
        result.status = "healthy"
        result.score = 100
        result.remediation = ""
        return result

    worst = max(result.issues, key=lambda i: _SEVERITY.get(i, 1))
    worst_severity = _SEVERITY.get(worst, 1)
    penalty = sum(_SEVERITY.get(i, 1) for i in result.issues) * 7
    result.score = max(0, 100 - penalty)
    result.status = "blocked" if worst_severity >= 4 else "degraded"
    result.remediation = _REMEDIATION.get(worst, "")
    # Issues are reported worst-first so a reader sees the blocker immediately.
    result.issues.sort(key=lambda i: _SEVERITY.get(i, 1), reverse=True)
    return result
