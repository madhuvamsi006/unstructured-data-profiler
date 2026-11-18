"""Unit tests for the profiling passes.

Run with: python -m pytest tests -q   (or: python tests/test_profiling.py)
"""

from __future__ import annotations

import sys
import zipfile
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from estate_profiler.connectors import open_connector  # noqa: E402
from estate_profiler.profiling import (  # noqa: E402
    dedupe,
    format_health,
    readiness,
    sensitivity,
    sniff,
    text_extract,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from make_sample_estate import _docx_bytes, _pdf_encrypted, _pdf_scanned, _pdf_with_text  # noqa: E402


# --------------------------------------------------------------------------
# sniff
# --------------------------------------------------------------------------

def test_sniff_recognises_common_formats():
    assert sniff.sniff(b"%PDF-1.7\n...", "pdf") == "pdf"
    assert sniff.sniff(b"\x89PNG\r\n\x1a\n", "png") == "png"
    assert sniff.sniff(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "doc") == "ole2"
    assert sniff.sniff(b"MZ\x90\x00", "exe") == "pe"
    assert sniff.sniff(b"", "txt") == "empty"
    assert sniff.sniff(b"plain readable content here", "txt") == "text"


def test_sniff_distinguishes_ooxml_families():
    assert sniff.sniff(_docx_bytes(["hello"]), "docx") == "ooxml-word"
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook/>")
    assert sniff.sniff(buffer.getvalue(), "xlsx") == "ooxml-excel"


def test_sniff_detects_binary_vs_text():
    assert sniff.sniff(b"\x00\x01\x02\x03" * 100, "") == "unknown-binary"


def test_extension_mismatch_is_detected():
    # A spreadsheet renamed to .pdf - the renamed-file case.
    assert sniff.extension_matches("pdf", "ooxml-excel") is False
    assert sniff.extension_matches("docx", "ooxml-word") is True
    # An extension that makes no checkable claim returns None, not False.
    assert sniff.extension_matches("xyz", "text") is None


# --------------------------------------------------------------------------
# text extraction
# --------------------------------------------------------------------------

def test_extracts_text_from_docx():
    data = _docx_bytes(["First paragraph of the agreement.", "Second paragraph follows."])
    result = text_extract.extract(data, "ooxml-word", "docx")
    assert result.extractable
    assert not result.needs_ocr
    assert "First paragraph" in result.text
    assert "Second paragraph" in result.text


def test_extracts_text_layer_from_digital_pdf():
    data = _pdf_with_text(["Governing law applies.", "Signed by both parties."])
    result = text_extract.extract(data, "pdf", "pdf")
    assert result.extractable
    assert result.method == "pdf-text-layer"
    assert "Governing law" in result.text


def test_scanned_pdf_yields_no_text_and_flags_ocr():
    result = text_extract.extract(_pdf_scanned(), "pdf", "pdf")
    assert not result.extractable
    assert result.needs_ocr
    assert result.method == "pdf-scanned-no-text-layer"


def test_encrypted_pdf_is_not_extractable_and_not_ocr():
    result = text_extract.extract(_pdf_encrypted(), "pdf", "pdf")
    assert not result.extractable
    # OCR cannot help with an encrypted file; recommending it would be wrong.
    assert not result.needs_ocr
    assert result.method == "pdf-encrypted"


def test_html_tags_are_stripped():
    html = b"<html><head><style>p{color:red}</style></head><body><p>Visible text</p></body></html>"
    result = text_extract.extract(html, "text", "html")
    assert "Visible text" in result.text
    assert "color:red" not in result.text
    assert "<p>" not in result.text


# --------------------------------------------------------------------------
# sensitivity
# --------------------------------------------------------------------------

def test_luhn_validator_rejects_invalid_card_numbers():
    assert sensitivity._luhn("4111111111111111")       # valid test number
    assert not sensitivity._luhn("4111111111111112")


def test_iban_and_aba_validators():
    assert sensitivity._iban("GB82WEST12345698765432")
    assert not sensitivity._iban("GB82WEST12345698765433")
    assert sensitivity._aba("021000021")
    assert not sensitivity._aba("021000022")


def test_verhoeff_validator():
    assert sensitivity._verhoeff("223456789012") == sensitivity._verhoeff("223456789012")
    assert not sensitivity._verhoeff("12345")      # wrong length


def test_credentials_are_detected_and_scored_critical_path():
    text = (
        "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n"
        "DATABASE_URL=postgresql://user:password123@db.internal:5432/app\n"
    )
    result = sensitivity.analyse(text, "deploy.env")
    assert result.has_credentials
    assert "aws_access_key" in result.findings
    assert result.level in ("high", "critical")


def test_context_boosting_separates_real_ssn_from_lookalike():
    with_context = sensitivity.analyse("Employee SSN 123-45-6789 on file", "hr.txt")
    without_context = sensitivity.analyse("Reference 123-45-6789 appears here", "misc.txt")
    assert with_context.score > without_context.score


def test_unvalidated_card_number_is_not_reported():
    result = sensitivity.analyse("Card 4111 1111 1111 1112 on file", "x.txt")
    assert "credit_card" not in result.findings


def test_clean_document_scores_zero():
    result = sensitivity.analyse("The quarterly planning meeting is on Tuesday.", "notes.txt")
    assert result.score == 0
    assert result.level == "none"


def test_phi_vocabulary_is_recognised():
    text = "Patient MRN 884512, diagnosis per ICD-10, prescription issued, lab result normal."
    result = sensitivity.analyse(text, "discharge.txt")
    assert "health" in result.categories


def test_dlp_merge_raises_score_and_marks_checked():
    base = sensitivity.analyse("nothing much here", "a.txt")
    merged = sensitivity.merge_dlp(base, {"US_SOCIAL_SECURITY_NUMBER": 3})
    assert merged.dlp_checked
    assert merged.score > 0
    assert "pii" in merged.categories


# --------------------------------------------------------------------------
# dedupe
# --------------------------------------------------------------------------

def _record(uri, text, size=1000, ts=1000.0, depth=1, path=None):
    return {
        "uri": uri,
        "rel_path": path or uri,
        "size_bytes": size,
        "depth": depth,
        "modified_ts": ts,
        "content_hash": dedupe.content_hash(text.encode()),
        "sketch": dedupe.sketch(text),
    }


# Representative of a real business document rather than a toy string: the
# similarity threshold is a ratio, so fixture length changes what it means.
_BODY = (
    "The migration programme consolidates regional file shares into a governed repository. "
    "Each business unit nominates a data steward responsible for confirming retention "
    "classification before content is moved. Content that cannot be classified within the "
    "review window is held in staging pending disposition. Throughput on the ingestion queue "
    "held steady against the agreed service level this period. Two vendor dependencies "
    "slipped their delivery dates and were escalated to the steering group for resolution. "
    "The regional rollout paused pending a decision on records retention in the Nordics. "
    "Load testing surfaced a contention issue in the metadata catalogue under concurrent "
    "writes, which the platform team is addressing in the next release train. Headcount for "
    "the data stewardship function remains one short of the approved plan. An interim "
    "reporting view was published while the warehouse model is being refactored. Security "
    "review closed three findings and deferred one to the following iteration. Cutover "
    "rehearsal completed with a shortened freeze window and no rollback was required. "
    "Stakeholder interviews for the taxonomy refresh finished across four business units. "
    "The archive tier migration ran overnight and reconciled against the source manifest. "
    "Licence consumption tracked slightly ahead of forecast and will be rebaselined shortly. "
)

_CHANGED_SENTENCE = (
    "Two vendor dependencies slipped their delivery dates and were escalated to the "
    "steering group for resolution."
)


def test_exact_duplicates_are_grouped_with_a_single_keeper():
    records = [
        _record("a", _BODY, ts=100.0, depth=3),
        _record("b", _BODY, ts=500.0, depth=1),   # newest and shallowest -> keeper
        _record("c", _BODY, ts=200.0, depth=2),
    ]
    report = dedupe.build_report(records)
    assert len(report.exact_groups) == 1
    assert report.exact_groups[0].keeper == "b"
    assert report.status_by_uri["b"] == "keeper"
    assert report.status_by_uri["a"] == "exact_duplicate"
    assert report.duplicate_file_count == 2
    assert report.exact_redundant_bytes == 2000


def test_near_duplicate_is_detected_when_one_sentence_changes():
    """The 'FINAL v2' case: same document, one clause edited, saved under a new name."""
    edited = _BODY.replace(
        _CHANGED_SENTENCE,
        "Legal confirmed the retention schedule applies to derived extracts as well.",
    )
    assert edited != _BODY
    report = dedupe.build_report([
        _record("orig", _BODY, ts=100.0),
        _record("v2", edited, ts=200.0),
    ])
    assert len(report.near_groups) == 1
    assert report.near_groups[0].similarity >= dedupe._NEAR_DUP_MIN_SIMILARITY
    assert report.near_groups[0].keeper == "v2"          # newest wins
    assert report.duplicate_file_count == 1


def test_substantial_rewrite_is_not_treated_as_a_near_duplicate():
    """Guards the other side of the threshold: a real rewrite must stay distinct.

    Half the document replaced is a new document, not a copy. If this starts
    clustering, the threshold has drifted far enough to collapse template
    families and the dedupe savings figure becomes fiction.
    """
    sentences = _BODY.split(". ")
    rewritten = ". ".join(sentences[: len(sentences) // 2]) + ". " + (
        "Initiate failover when the primary region has been unreachable for four minutes. "
        "Freeze writes at the edge and promote the standby replica to serve traffic. "
        "Confirm its log position matches the last acknowledged transaction before cutover. "
        "Repoint the service mesh and drain the retired endpoints in sequence. "
        "Record the achieved recovery point and recovery time after every rehearsal. "
        "Raise a change request for anything that had to be performed manually. "
    )
    report = dedupe.build_report([
        _record("a", _BODY),
        _record("b", rewritten),
    ])
    assert report.near_groups == []


def test_unrelated_documents_are_not_clustered():
    other = (
        "Initiate failover when the primary region has been unreachable for four minutes. "
        "Freeze writes at the edge, promote the standby replica, and confirm its log position "
        "matches the last acknowledged transaction before repointing the service mesh. "
        "Record the achieved recovery point and recovery time after every rehearsal exercise. "
    )
    report = dedupe.build_report([
        _record("a", _BODY),
        _record("b", other),
    ])
    assert report.near_groups == []
    assert report.duplicate_file_count == 0


def test_similarity_is_one_for_identical_text_and_low_for_different():
    same = dedupe.sketch(_BODY)
    assert dedupe.similarity(same, same) == 1.0
    other = dedupe.sketch("Completely different content about unrelated subject matter " * 6)
    assert dedupe.similarity(same, other) < 0.2


def test_short_text_produces_no_sketch():
    assert dedupe.sketch("too short to shingle") is None


# --------------------------------------------------------------------------
# format health
# --------------------------------------------------------------------------

def _health(ext, sniffed, size, extraction, age=None):
    return format_health.assess(
        ext=ext, sniffed=sniffed, size_bytes=size, extraction=extraction, age_days=age
    )


def test_zero_byte_file_is_blocked():
    result = _health("docx", "empty", 0, text_extract.Extraction("", "empty-file", False, False))
    assert result.status == "blocked"
    assert "zero_byte" in result.issues
    assert not result.processable


def test_extension_mismatch_is_flagged():
    extraction = text_extract.Extraction("data", "ooxml-excel-xml", True, False)
    result = _health("pdf", "ooxml-excel", 5000, extraction)
    assert "extension_mismatch" in result.issues
    assert result.status == "blocked"


def test_scanned_pdf_is_degraded_not_blocked():
    extraction = text_extract.Extraction("", "pdf-scanned-no-text-layer", False, True)
    result = _health("pdf", "pdf", 50_000, extraction)
    assert "needs_ocr" in result.issues
    assert result.status == "degraded"
    assert "Document AI" in result.remediation


def test_healthy_file_has_no_issues():
    extraction = text_extract.Extraction("lots of text", "ooxml-word-xml", True, False)
    result = _health("docx", "ooxml-word", 20_000, extraction, age=30)
    assert result.status == "healthy"
    assert result.issues == []
    assert result.score == 100


def test_stale_file_is_flagged():
    extraction = text_extract.Extraction("text", "plain-text", True, False)
    result = _health("txt", "text", 500, extraction, age=4000)
    assert "stale" in result.issues


# --------------------------------------------------------------------------
# readiness
# --------------------------------------------------------------------------

def _score(extraction, health, sens, dup_status="unique", sniffed="ooxml-word", ext="docx"):
    return readiness.score(
        extraction=extraction, health=health, sensitivity=sens,
        dup_status=dup_status, sniffed=sniffed, ext=ext,
    )


def test_clean_document_is_ingest_now():
    extraction = text_extract.Extraction("word " * 500, "ooxml-word-xml", True, False)
    health = _health("docx", "ooxml-word", 30_000, extraction, age=30)
    result = _score(extraction, health, sensitivity.analyse("word " * 500, "a.docx"))
    assert result.disposition == "ingest_now"
    assert result.band == "ready"
    assert result.recommended_processor == readiness.PROCESSOR_NONE


def test_scanned_pdf_routes_to_document_ai():
    extraction = text_extract.Extraction("", "pdf-scanned-no-text-layer", False, True)
    health = _health("pdf", "pdf", 400_000, extraction, age=100)
    result = _score(extraction, health, sensitivity.SensitivityResult(), sniffed="pdf", ext="pdf")
    assert result.disposition == "ingest_after_processing"
    assert result.recommended_processor == readiness.PROCESSOR_OCR


def test_form_like_scan_routes_to_form_parser():
    extraction = text_extract.Extraction("", "pdf-scanned-no-text-layer", False, True)
    health = _health("pdf", "pdf", 400_000, extraction)
    result = _score(extraction, health, sensitivity.SensitivityResult(), sniffed="pdf", ext="invoice")
    assert result.recommended_processor == readiness.PROCESSOR_FORM


def test_credentials_force_quarantine_regardless_of_score():
    text = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n" + "word " * 500
    extraction = text_extract.Extraction(text, "plain-text", True, False)
    health = _health("env", "text", 2000, extraction, age=10)
    result = _score(extraction, health, sensitivity.analyse(text, "deploy.env"), ext="env", sniffed="text")
    assert result.disposition == "quarantine"
    assert "credential" in result.blocking_reason.lower()


def test_exact_duplicate_is_excluded():
    extraction = text_extract.Extraction("word " * 500, "ooxml-word-xml", True, False)
    health = _health("docx", "ooxml-word", 30_000, extraction, age=30)
    result = _score(extraction, health, sensitivity.SensitivityResult(), dup_status="exact_duplicate")
    assert result.disposition == "exclude"
    assert result.dimensions["cleanliness"] == 0


def test_encrypted_file_is_quarantined():
    extraction = text_extract.Extraction("", "pdf-encrypted", False, False)
    health = _health("pdf", "pdf", 90_000, extraction)
    result = _score(extraction, health, sensitivity.SensitivityResult(), sniffed="pdf", ext="pdf")
    assert result.disposition == "quarantine"
    assert result.dimensions["extractability"] == 0


def test_token_and_chunk_estimates_are_produced():
    text = "word " * 2000
    extraction = text_extract.Extraction(text, "plain-text", True, False)
    health = _health("txt", "text", 10_000, extraction, age=10)
    result = _score(extraction, health, sensitivity.SensitivityResult(), sniffed="text", ext="txt")
    assert result.est_tokens > 0
    assert result.est_chunks >= 1


# --------------------------------------------------------------------------
# connectors
# --------------------------------------------------------------------------

def test_gcs_uri_is_rejected_without_bucket():
    try:
        open_connector("gs://")
    except ValueError as exc:
        assert "Malformed GCS URI" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_unsupported_scheme_is_rejected_with_guidance():
    try:
        open_connector("s3://my-bucket/prefix")
    except ValueError as exc:
        assert "not supported in v1" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_missing_local_path_is_rejected():
    try:
        open_connector("./definitely-not-a-real-directory-xyz")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError")


if __name__ == "__main__":
    import traceback

    tests = [(n, o) for n, o in sorted(globals().items()) if n.startswith("test_") and callable(o)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception:
            failures += 1
            print(f"  FAIL  {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
