"""Document AI / RAG readiness scoring.

Readiness answers the question a migration lead actually asks: *if I point an
ingestion pipeline at this estate tomorrow, what fraction of it lands cleanly,
what needs a Document AI processor first, and what should never go in?*

The score is a weighted blend of five dimensions, each 0-100:

  extractability  can text be obtained, and at what cost (native > OCR > never)
  substance       is there enough real text to be worth retrieving
  chunkability    does it split into sensible retrieval units
  cleanliness     is it a unique document rather than the fourth copy of one
  governance      can it be indexed without creating an exposure problem

Weights are module constants so an engagement can retune them — a regulated
client will weight governance far higher than a general knowledge-base build.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .format_health import HealthResult
from .sensitivity import SensitivityResult
from .text_extract import Extraction

WEIGHTS = {
    "extractability": 0.32,
    "substance": 0.22,
    "chunkability": 0.14,
    "cleanliness": 0.16,
    "governance": 0.16,
}

# Rough token estimate; good enough for sizing retrieval units.
_CHARS_PER_TOKEN = 4
_IDEAL_TOKEN_RANGE = (200, 120_000)
_MIN_USEFUL_CHARS = 200

# Document AI processors the report can recommend, in escalating capability.
PROCESSOR_NONE = "none-native-text"
PROCESSOR_OCR = "document-ocr"
PROCESSOR_LAYOUT = "layout-parser"
PROCESSOR_FORM = "form-parser"

DISPOSITIONS = ("ingest_now", "ingest_after_processing", "remediate", "quarantine", "exclude")


@dataclass
class ReadinessResult:
    score: int = 0
    band: str = "not_ready"              # ready | conditional | needs_work | not_ready
    disposition: str = "exclude"
    dimensions: dict[str, int] = field(default_factory=dict)
    recommended_processor: str = PROCESSOR_NONE
    est_tokens: int = 0
    est_chunks: int = 0
    rationale: list[str] = field(default_factory=list)
    blocking_reason: str = ""


def _band(score: int) -> str:
    if score >= 75:
        return "ready"
    if score >= 50:
        return "conditional"
    if score >= 25:
        return "needs_work"
    return "not_ready"


def _extractability(extraction: Extraction, health: HealthResult) -> tuple[int, str]:
    if "encrypted" in health.issues:
        return 0, "Encrypted: no pipeline can read it without credentials."
    if "corrupt" in health.issues or "zero_byte" in health.issues:
        return 0, "No readable content."
    if "not_a_document" in health.issues:
        return 0, "Not a document: binary or application data."
    if "archive_unexpanded" in health.issues:
        return 15, "Archive: contents must be expanded before they can be assessed."
    if "media_needs_transcription" in health.issues:
        return 20, "Audio/video: needs transcription, not document processing."
    if "legacy_format" in health.issues:
        return 40, "Legacy binary format: convert before ingest."
    if extraction.needs_ocr:
        return 55, "No text layer: recoverable with Document AI OCR."
    if extraction.extractable and extraction.char_count >= _MIN_USEFUL_CHARS:
        return 100, "Native text layer: ingests directly."
    if extraction.extractable:
        return 70, "Text layer present but very short."
    return 10, f"Text extraction failed ({extraction.method})."


def _substance(extraction: Extraction) -> tuple[int, str]:
    chars = extraction.char_count
    if chars == 0:
        return 0, "No extractable text."
    if chars < 50:
        return 10, "Negligible text content."
    if chars < _MIN_USEFUL_CHARS:
        return 35, "Very little text; likely a stub or label."
    if chars < 1_000:
        return 65, "Short document."
    if chars < 500_000:
        return 100, "Substantial text content."
    return 80, "Very large text body; will need aggressive chunking."


def _chunkability(est_tokens: int, extraction: Extraction) -> tuple[int, str]:
    if est_tokens == 0:
        return 0, "Nothing to chunk."
    low, high = _IDEAL_TOKEN_RANGE
    if est_tokens < low:
        return 40, "Too small to chunk meaningfully; consider merging with siblings."
    if est_tokens <= high:
        return 100, "Chunks cleanly into retrieval units."
    if est_tokens <= high * 5:
        return 70, "Large document; needs structure-aware chunking."
    return 45, "Very large; split into sub-documents before indexing."


def _cleanliness(dup_status: str) -> tuple[int, str]:
    return {
        "unique": (100, "Unique content."),
        "keeper": (95, "Canonical copy of a duplicate set."),
        "near_duplicate": (30, "Near-duplicate of another file; indexing both splits retrieval."),
        "exact_duplicate": (0, "Exact duplicate; indexing it wastes storage and dilutes results."),
    }.get(dup_status, (80, "Duplicate status unknown."))


def _governance(sensitivity: SensitivityResult) -> tuple[int, str]:
    if sensitivity.has_credentials:
        return 0, "Contains live credentials; must be remediated, never indexed as-is."
    return {
        "critical": (10, "Critical sensitivity: redaction or exclusion required before indexing."),
        "high": (35, "High sensitivity: index only behind access controls with redaction."),
        "medium": (65, "Moderate sensitivity: apply ACL inheritance at index time."),
        "low": (85, "Minor sensitivity signals."),
        "none": (100, "No sensitive content detected."),
    }.get(sensitivity.level, (80, ""))


_FORM_HINTS = (
    "form", "invoice", "receipt", "application", "claim", "w-2", "w2", "1099",
    "statement", "remittance", "purchase order", "po_", "bill",
)


def _recommend_processor(
    extraction: Extraction, sniffed: str, ext: str, text: str, name: str = ""
) -> str:
    if extraction.needs_ocr:
        # Forms benefit from the richer parser; prose only needs an OCR text layer.
        # The filename matters most here: a file that needs OCR has no text to
        # read, so its name is usually the only evidence of what it is.
        haystack = f"{name} {ext} {text[:2000]}".lower()
        if any(hint in haystack for hint in _FORM_HINTS):
            return PROCESSOR_FORM
        return PROCESSOR_OCR
    if sniffed in ("ooxml-excel", "odf-sheet") or ext in ("xlsx", "xls", "csv"):
        return PROCESSOR_LAYOUT
    if extraction.method == "pdf-text-layer" and extraction.page_hint > 10:
        return PROCESSOR_LAYOUT
    return PROCESSOR_NONE


def score(
    *,
    extraction: Extraction,
    health: HealthResult,
    sensitivity: SensitivityResult,
    dup_status: str,
    sniffed: str,
    ext: str,
    name: str = "",
) -> ReadinessResult:
    result = ReadinessResult()

    est_tokens = max(0, extraction.char_count // _CHARS_PER_TOKEN)
    result.est_tokens = est_tokens
    # 800-token chunks with 15% overlap is a common default; close enough to size a corpus.
    result.est_chunks = max(1, round(est_tokens / 680)) if est_tokens else 0

    dimension_scores: dict[str, int] = {}
    rationale: list[str] = []
    for name, fn, args in (
        ("extractability", _extractability, (extraction, health)),
        ("substance", _substance, (extraction,)),
        ("chunkability", _chunkability, (est_tokens, extraction)),
        ("cleanliness", _cleanliness, (dup_status,)),
        ("governance", _governance, (sensitivity,)),
    ):
        value, reason = fn(*args)
        dimension_scores[name] = value
        if reason:
            rationale.append(reason)

    result.dimensions = dimension_scores
    result.score = round(sum(dimension_scores[k] * w for k, w in WEIGHTS.items()))
    result.band = _band(result.score)
    result.rationale = rationale
    result.recommended_processor = _recommend_processor(
        extraction, sniffed, ext, extraction.text, name
    )

    # Disposition is a decision, not a score: certain conditions override the blend.
    if sensitivity.has_credentials:
        result.disposition = "quarantine"
        result.blocking_reason = "Live credentials detected."
    elif dup_status == "exact_duplicate":
        result.disposition = "exclude"
        result.blocking_reason = "Exact duplicate of a retained file."
    elif dimension_scores["extractability"] == 0:
        result.disposition = "quarantine" if "encrypted" in health.issues or "corrupt" in health.issues else "exclude"
        result.blocking_reason = health.remediation or "No retrievable content."
    elif sensitivity.level == "critical":
        result.disposition = "remediate"
        result.blocking_reason = "Critical sensitivity requires redaction before indexing."
    elif extraction.needs_ocr or "legacy_format" in health.issues or "archive_unexpanded" in health.issues:
        result.disposition = "ingest_after_processing"
        result.blocking_reason = health.remediation
    elif health.status == "blocked":
        result.disposition = "remediate"
        result.blocking_reason = health.remediation
    elif result.score >= 50:
        result.disposition = "ingest_now"
    else:
        result.disposition = "remediate"
        result.blocking_reason = "Low readiness across extraction and substance."

    return result
