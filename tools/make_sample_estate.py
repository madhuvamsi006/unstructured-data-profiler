"""Generate a synthetic 'messy estate' for demos and tests.

Deliberately plants every condition the profiler is supposed to catch:
exact duplicates, near-duplicates, planted (fake) secrets and PII, a scanned
PDF with no text layer, an encrypted PDF, legacy Office binaries, a renamed
file whose extension lies, zero-byte files, and stale content.

All identifiers are synthetic and checksum-valid-by-construction test values.
"""

from __future__ import annotations

import os
import random
import shutil
import sys
import time
import zipfile
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

random.seed(1729)

LOREM = (
    "The migration programme covers the consolidation of regional file shares into a single "
    "governed repository. Each business unit nominates a data steward responsible for "
    "confirming retention classification before any content is moved. Content that cannot be "
    "classified within the review window is held in a staging area pending disposition. "
)


def _docx_bytes(paragraphs: list[str]) -> bytes:
    """Minimal but genuinely valid .docx."""
    body = "".join(
        f"<w:p><w:r><w:t xml:space='preserve'>{p}</w:t></w:r></w:p>" for p in paragraphs
    )
    document = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
        f"<w:body>{body}</w:body></w:document>"
    )
    content_types = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'>"
        "<Default Extension='xml' ContentType='application/xml'/>"
        "<Default Extension='rels' ContentType='application/vnd.openxmlformats-package.relationships+xml'/>"
        "<Override PartName='/word/document.xml' ContentType='application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'/>"
        "</Types>"
    )
    rels = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>"
        "<Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument' Target='word/document.xml'/>"
        "</Relationships>"
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def _xlsx_bytes(strings: list[str]) -> bytes:
    shared = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        "<sst xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main' "
        f"count='{len(strings)}' uniqueCount='{len(strings)}'>"
        + "".join(f"<si><t>{s}</t></si>" for s in strings)
        + "</sst>"
    )
    workbook = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        "<workbook xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'>"
        "<sheets><sheet name='Sheet1' sheetId='1' r:id='rId1' "
        "xmlns:r='http://schemas.openxmlformats.org/officeDocument/2006/relationships'/></sheets></workbook>"
    )
    sheet = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        "<worksheet xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'>"
        "<sheetData><row r='1'><c r='A1' t='s'><v>0</v></c></row></sheetData></worksheet>"
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/sharedStrings.xml", shared)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


def _pdf_with_text(lines: list[str]) -> bytes:
    """Uncompressed single-page PDF with a real text layer."""
    content = "BT /F1 12 Tf 72 720 Td 14 TL\n"
    for line in lines:
        escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        content += f"({escaped}) Tj T*\n"
    content += "ET"
    stream = content.encode("latin-1", errors="replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n".encode()
        + b"%%EOF\n"
    )
    return bytes(out)


def _pdf_scanned() -> bytes:
    """A PDF whose only content is an image XObject — the classic OCR case."""
    image = bytes(random.getrandbits(8) for _ in range(2048))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /XObject << /Im0 5 0 R >> >> >>",
        b"<< /Length 44 >>\nstream\nq 612 0 0 792 0 0 cm /Im0 Do Q\nendstream",
        b"<< /Type /XObject /Subtype /Image /Width 1700 /Height 2200 /ColorSpace /DeviceGray "
        b"/BitsPerComponent 8 /Filter /DCTDecode /Length " + str(len(image)).encode() + b" >>\nstream\n"
        + image + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    for index, body in enumerate(objects, start=1):
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    out += b"trailer\n<< /Size 6 /Root 1 0 R >>\n%%EOF\n"
    return bytes(out)


def _pdf_encrypted() -> bytes:
    return (
        b"%PDF-1.6\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"6 0 obj\n<< /Filter /Standard /V 4 /R 4 /Length 128 /P -1340 >>\nendobj\n"
        b"trailer\n<< /Size 7 /Root 1 0 R /Encrypt 6 0 R >>\n%%EOF\n"
    )


def _ole2_legacy(payload: str) -> bytes:
    """OLE2 compound-file header — what a real .doc/.xls starts with."""
    return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504 + payload.encode("latin-1", "replace")


def _write(path: Path, data: bytes, age_days: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if age_days:
        stamp = (datetime.now() - timedelta(days=age_days)).timestamp()
        os.utime(path, (stamp, stamp))


def build(root: Path) -> None:
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    # --- Finance: contracts, invoices, planted financial PII ---------------
    fin = root / "Finance"
    contract = [
        "MASTER SERVICES AGREEMENT",
        "This Agreement is entered into by the parties identified below.",
        "The party of the first part shall indemnify the other against claims.",
        "Governing law: the laws of the State of Delaware apply to this agreement.",
        "Confidentiality agreement provisions survive term and termination.",
        LOREM,
    ]
    _write(fin / "Contracts" / "MSA_Acme_2023.pdf", _pdf_with_text(contract), age_days=400)
    # Near-duplicate: same contract, one clause changed and a new title.
    contract_v2 = list(contract)
    contract_v2[3] = "Governing law: the laws of the State of New York apply to this agreement."
    _write(fin / "Contracts" / "MSA_Acme_2023_FINAL_v2.pdf", _pdf_with_text(contract_v2), age_days=380)
    _write(fin / "Contracts" / "archive" / "MSA_Acme_2023 (copy).pdf", _pdf_with_text(contract), age_days=395)

    invoice_rows = [
        "Invoice INV-20481 | Purchase order PO-9912 | Remittance advice attached",
        "Accounts payable contact: ap.team@acme-demo.test | +1 415-555-0142",
        "Payment card on file: 4111 1111 1111 1111 expiry 09/27 CVV handled separately",
        "Bank account IBAN GB82WEST12345698765432 | routing 021000021",
        "VAT registered; GST applied for APAC entities. Balance sheet reference Q3.",
    ]
    _write(fin / "AP" / "invoice_batch_q3.xlsx", _xlsx_bytes(invoice_rows), age_days=120)
    _write(fin / "AP" / "remittance_notes.txt",
           ("\n".join(invoice_rows) + "\n" + LOREM * 3).encode(), age_days=119)
    _write(fin / "Legacy" / "budget_fy19.xls", _ole2_legacy("Budget FY19 ledger"), age_days=2400)

    # --- HR: PII-heavy, the classic sensitivity hotspot ---------------------
    hr = root / "HR"
    roster = [
        "EMPLOYEE ROSTER - CONFIDENTIAL",
        "Name: Dana Whitfield | Employee ID 44821 | DOB 14/03/1987",
        "SSN 123-45-6789 | salary band 7 | performance review scheduled",
        "Name: Rohan Bhatt | PAN ABCDE1234F | Aadhaar 2234 5678 9012",
        "Name: Priya Raman | national insurance QQ123456C | offer letter issued",
        "Payroll processed monthly. Compensation adjustments require approval.",
        LOREM,
    ]
    _write(hr / "Payroll" / "employee_roster_2024.docx", _docx_bytes(roster), age_days=60)
    _write(hr / "Payroll" / "employee_roster_2024 - Copy.docx", _docx_bytes(roster), age_days=58)
    _write(hr / "Reviews" / "performance_notes.txt",
           ("Performance review cycle notes.\n\n"
            "Appraisal ratings are pending for the regional teams. Calibration sessions ran "
            "across three time zones and surfaced an inconsistency in how the middle band is "
            "applied. Compensation adjustments require approval from the remuneration committee "
            "before any letters are issued. Managers were reminded that review records are "
            "retained for seven years under the current schedule.\n").encode(), age_days=200)
    _write(hr / "Scans" / "signed_offer_letter.pdf", _pdf_scanned(), age_days=90)
    _write(hr / "Scans" / "id_card_scan.png",
           b"\x89PNG\r\n\x1a\n" + bytes(random.getrandbits(8) for _ in range(4096)), age_days=90)

    # --- Clinical: PHI vocabulary -------------------------------------------
    clinical = root / "Clinical"
    phi = [
        "PATIENT DISCHARGE SUMMARY",
        "Patient MRN 884512 presented with elevated blood pressure.",
        "Diagnosis coded per ICD-10. Treatment plan and dosage recorded below.",
        "Lab result: glucose 142 mg/dL. Allergies documented. Prescription issued.",
        "Health insurance claim submitted; HIPAA notice acknowledged by patient.",
        LOREM * 2,
    ]
    _write(clinical / "Discharge" / "summary_2024_0412.pdf", _pdf_with_text(phi), age_days=210)
    _write(clinical / "Discharge" / "summary_2024_0412_scanned.pdf", _pdf_scanned(), age_days=210)

    # --- IT: planted credentials (all syntactically valid, none real) -------
    it = root / "IT"
    _write(it / "configs" / "deploy.env",
           b"DB_HOST=10.20.30.40\n"
           b"DB_PASSWORD=sup3rs3cret-demo-value\n"
           b"AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n"
           b"API_KEY=AIzaSyA1234567890abcdefghijklmnopqrstu\n"
           b"DATABASE_URL=postgresql://svcuser:hunter2demo@db.internal:5432/app\n", age_days=30)
    _write(it / "configs" / "service-account.json",
           b'{\n  "type": "service_account",\n  "project_id": "demo-project",\n'
           b'  "private_key": "-----BEGIN PRIVATE KEY-----\\nMIIEvQIBADANB...\\n-----END PRIVATE KEY-----\\n",\n'
           b'  "client_email": "svc@demo-project.iam.gserviceaccount.test"\n}\n', age_days=30)
    _write(it / "runbooks" / "failover_runbook.md",
           ("# Failover Runbook\n\n"
            "## Trigger conditions\n\nInitiate failover when the primary region has been "
            "unreachable for more than four minutes, or when write latency exceeds the agreed "
            "threshold for three consecutive sampling windows.\n\n"
            "## Sequence\n\n1. Freeze writes at the edge.\n2. Promote the standby replica and "
            "confirm its log position matches the last acknowledged transaction.\n"
            "3. Repoint the service mesh and drain the old endpoints.\n"
            "4. Verify the reconciliation job completes before lifting the write freeze.\n\n"
            "## After the event\n\nRecord the achieved recovery point and recovery time, and "
            "raise a change request for anything that had to be done manually.\n").encode(),
           age_days=150)

    # --- Legal hold ----------------------------------------------------------
    legal = root / "Legal"
    _write(legal / "matter_1182" / "litigation_hold_notice.docx",
           _docx_bytes([
               "LEGAL HOLD NOTICE - PRIVILEGED AND CONFIDENTIAL",
               "This matter is subject to litigation and a subpoena has been served.",
               "Attorney-client privileged communications must be preserved.",
               LOREM * 2,
           ]), age_days=45)

    # --- Problem files -------------------------------------------------------
    junk = root / "Shared" / "_unsorted"
    _write(junk / "empty_placeholder.docx", b"")
    _write(junk / "notes",                                                  # no extension
           ("Handover notes from the decommissioned intranet. The team that owned this share "
            "moved under a different directorate in 2021 and no steward was reappointed. Most "
            "of the content below predates the current classification scheme and will need a "
            "retention decision before it can be moved anywhere.\n").encode(), age_days=800)
    _write(junk / "quarterly_report.pdf", _xlsx_bytes(["actually a spreadsheet"]))  # extension lies
    _write(junk / "locked_board_pack.pdf", _pdf_encrypted(), age_days=20)
    _write(junk / "archive_2016.zip", _make_zip(), age_days=3300)
    _write(junk / "truncated.docx", b"PK\x03\x04" + b"\x00" * 64)          # corrupt OOXML
    _write(junk / "setup_installer.exe", b"MZ" + b"\x00" * 2048)
    _write(junk / "team_offsite.mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8192)
    _write(junk / "readme.txt", b"ok\n", age_days=2000)                     # too small to be useful

    # --- Bulk: realistic long tail with genuinely distinct content ----------
    # Each report draws a different subset of sentences, so the only things that
    # cluster are the duplicates planted below — not the whole template family.
    bulk = root / "Projects"
    sentences = [
        "Throughput on the ingestion queue held steady against the agreed service level.",
        "Two vendor dependencies slipped their delivery dates and were escalated to the steering group.",
        "The regional rollout paused pending a decision on records retention in the Nordics.",
        "Load testing surfaced a contention issue in the metadata catalogue under concurrent writes.",
        "Headcount for the data stewardship function remains one short of the approved plan.",
        "An interim reporting view was published while the warehouse model is refactored.",
        "Security review closed three findings and deferred one to the next release train.",
        "Cutover rehearsal completed with a shortened freeze window and no rollback required.",
        "Stakeholder interviews for the taxonomy refresh finished across four business units.",
        "The archive tier migration ran overnight and reconciled against the source manifest.",
        "A defect in the duplicate-detection rule caused false positives on template documents.",
        "Training materials were rewritten after feedback that the walkthrough assumed too much.",
        "Licence consumption tracked slightly ahead of forecast and will be rebaselined.",
        "The disaster recovery test met its recovery point objective but missed recovery time by eleven minutes.",
        "Legal confirmed that the retention schedule applies to derived extracts as well as source records.",
        "Network egress charges rose after the reporting workload moved regions.",
        "A backlog of unclassified documents built up while the steward role was vacant.",
        "The interface to the case management system was deprecated and replaced with an event feed.",
    ]
    units = ["alpha", "bravo", "charlie", "delta"]
    for index in range(40):
        unit = units[index % 4]
        picks = random.sample(sentences, 6)
        body = [
            f"Project {unit.title()} — status report, week {index + 1}",
            f"Reporting period ending day {random.randint(1, 28)}. "
            f"Open risks: {random.randint(1, 9)}. Milestone variance {random.randint(-5, 5)} days. "
            f"Budget consumed to date: {random.randint(12, 94)}%.",
            " ".join(picks[:3]),
            " ".join(picks[3:]),
            f"Next period focus: {random.choice(picks)} Owner: steward {random.randint(100, 999)}.",
        ]
        _write(bulk / unit / f"status_week_{index + 1:02d}.docx",
               _docx_bytes(body), age_days=random.randint(10, 1200))

    # Exact duplicates: the same report copied to three other places.
    duplicate_body = [
        "Project Alpha — status report, week 1",
        "Reporting period ending day 7. Open risks: 3. Milestone variance 0 days. Budget consumed to date: 14%.",
        " ".join(sentences[:3]),
        " ".join(sentences[3:6]),
        "Next period focus: close the outstanding vendor escalation. Owner: steward 204.",
    ]
    duplicate = _docx_bytes(duplicate_body)
    for copy_path in ("_shared/status_week_01.docx",
                      "_shared/backup/status_week_01.docx",
                      "_shared/backup/status_week_01 (1).docx"):
        _write(bulk / copy_path, duplicate, age_days=300)

    # Near-duplicate: the same report with one sentence swapped and a new name —
    # the "FINAL v2" pattern the SimHash pass is built to catch.
    near_body = list(duplicate_body)
    near_body[4] = "Next period focus: confirm the retention schedule with Legal. Owner: steward 204."
    _write(bulk / "_shared" / "status_week_01_REVISED.docx", _docx_bytes(near_body), age_days=295)


def _make_zip() -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("old/notes.txt", LOREM * 2)
        archive.writestr("old/ledger.csv", "id,amount\n1,100\n2,250\n")
    return buffer.getvalue()


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "sample_estate").resolve()
    build(target)
    files = sum(1 for _ in target.rglob("*") if _.is_file())
    total = sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
    print(f"Sample estate written to {target}: {files} files, {total / 1024:.1f} KB")
