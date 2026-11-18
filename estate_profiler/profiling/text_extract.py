"""Best-effort text extraction using only the standard library.

This is deliberately *not* a document parser. Triage needs to answer one
question per file — "is there retrievable text in here, and roughly how much?"
— cheaply and over an entire estate. Where the answer is "not without OCR",
that is the finding, and Document AI is the remediation the report recommends.

OOXML (.docx/.xlsx/.pptx) and ODF are ZIP+XML, so stdlib ``zipfile`` reads them
in full. PDFs get a shallow stream scan that recovers text from digitally
authored files and deliberately returns nothing for scanned ones.
"""

from __future__ import annotations

import html
import re
import zipfile
import zlib
from dataclasses import dataclass
from io import BytesIO

_XML_TAG = re.compile(rb"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_MULTI_NL = re.compile(r"\n{3,}")
_SCRIPT_STYLE = re.compile(rb"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
# Text-showing operators in a PDF content stream: (literal) Tj and [..] TJ.
_PDF_TJ = re.compile(rb"\((?:\\.|[^\\()])*\)")
_PDF_STREAM = re.compile(rb"stream\r?\n(.*?)endstream", re.DOTALL)


@dataclass
class Extraction:
    """Result of trying to pull text out of one file."""

    text: str
    method: str          # how the text was obtained, or why it wasn't
    extractable: bool    # did we get usable text without OCR?
    needs_ocr: bool      # would Document AI OCR plausibly unlock this?
    page_hint: int = 0   # pages/sheets/slides when cheaply known

    @property
    def char_count(self) -> int:
        return len(self.text)


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _clean(text: str) -> str:
    text = _WS.sub(" ", text)
    text = _MULTI_NL.sub("\n\n", text)
    return text.strip()


def _strip_xml(data: bytes, block_tags: tuple[bytes, ...] = ()) -> str:
    """Drop XML markup, inserting newlines where block tags imply breaks."""
    for tag in block_tags:
        data = data.replace(tag, b"\n" + tag)
    return _clean(html.unescape(_XML_TAG.sub(b" ", data).decode("utf-8", errors="replace")))


def extract(data: bytes, sniffed: str, ext: str) -> Extraction:
    """Dispatch on sniffed type; ``data`` may be a truncated head of the file."""
    if not data:
        return Extraction("", "empty-file", extractable=False, needs_ocr=False)

    if sniffed in ("ooxml-word", "ooxml-excel", "ooxml-ppt", "odf-text", "odf-sheet", "odf-presentation", "odf"):
        return _extract_ooxml(data, sniffed)
    if sniffed == "ooxml-encrypted":
        return Extraction("", "encrypted-ooxml", extractable=False, needs_ocr=False)
    if sniffed == "pdf":
        return _extract_pdf(data)
    if sniffed == "rtf":
        return _extract_rtf(data)
    if sniffed in ("text", "pem"):
        return _extract_text(data, ext)
    if sniffed in ("png", "jpeg", "gif", "bmp", "tiff", "webp"):
        return Extraction("", "image-no-text-layer", extractable=False, needs_ocr=True)
    if sniffed == "ole2":
        # Legacy Office binaries need conversion before any pipeline can read them.
        return Extraction("", "legacy-binary-office", extractable=False, needs_ocr=False)
    if sniffed in ("zip", "rar", "7z", "gzip"):
        return Extraction("", "archive-needs-expansion", extractable=False, needs_ocr=False)
    if sniffed in ("mp4", "mp3", "wav", "avi", "ogg", "flac", "matroska", "mpeg", "riff"):
        return Extraction("", "media-needs-transcription", extractable=False, needs_ocr=False)
    if sniffed in ("pe", "elf", "sqlite", "unknown-binary"):
        return Extraction("", f"not-a-document:{sniffed}", extractable=False, needs_ocr=False)
    return Extraction("", f"unhandled:{sniffed}", extractable=False, needs_ocr=False)


def _extract_text(data: bytes, ext: str) -> Extraction:
    raw = _decode(data)
    if ext in ("html", "htm", "xhtml") or raw.lstrip()[:200].lower().startswith(("<!doctype html", "<html")):
        body = _SCRIPT_STYLE.sub(b" ", data)
        return Extraction(_strip_xml(body, (b"<p", b"<br", b"<div", b"<li", b"<tr", b"<h1", b"<h2", b"<h3")),
                          "html-strip", extractable=True, needs_ocr=False)
    if ext in ("xml",):
        return Extraction(_strip_xml(data), "xml-strip", extractable=True, needs_ocr=False)
    return Extraction(_clean(raw), "plain-text", extractable=True, needs_ocr=False)


def _extract_ooxml(data: bytes, sniffed: str) -> Extraction:
    """Read the XML parts that hold user-visible text."""
    # Members whose text is content, keyed by document family.
    wanted = {
        "ooxml-word": ("word/document.xml", "word/footnotes.xml", "word/endnotes.xml"),
        "ooxml-excel": ("xl/sharedStrings.xml",),
        "ooxml-ppt": (),  # slide parts are enumerated below
    }.get(sniffed, ())

    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            names = archive.namelist()
            targets: list[str] = [n for n in wanted if n in names]
            page_hint = 0

            if sniffed == "ooxml-ppt":
                slides = sorted(n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
                notes = sorted(n for n in names if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", n))
                targets = slides + notes
                page_hint = len(slides)
            elif sniffed == "ooxml-excel":
                page_hint = sum(1 for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
                # Inline strings live in the sheets rather than sharedStrings.
                targets += sorted(n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))[:25]
            elif sniffed.startswith("odf"):
                targets = [n for n in ("content.xml",) if n in names]

            if not targets and "content.xml" in names:
                targets = ["content.xml"]
            if not targets:
                return Extraction("", f"{sniffed}-no-text-parts", extractable=False, needs_ocr=False)

            chunks: list[str] = []
            for member in targets:
                try:
                    chunks.append(_strip_xml(archive.read(member), (b"<w:p ", b"<w:p>", b"<a:p>", b"<text:p", b"</row>")))
                except Exception:
                    continue
    except zipfile.BadZipFile:
        # Truncated sample of a large document: we read a head, not the whole file.
        return Extraction("", f"{sniffed}-truncated-sample", extractable=False, needs_ocr=False)
    except Exception:
        return Extraction("", f"{sniffed}-unreadable", extractable=False, needs_ocr=False)

    text = _clean("\n".join(c for c in chunks if c))
    return Extraction(text, f"{sniffed}-xml", extractable=bool(text), needs_ocr=False, page_hint=page_hint)


def _extract_pdf(data: bytes) -> Extraction:
    """Pull literal strings out of content streams.

    Recovers text from digitally authored PDFs. A scanned PDF has image XObjects
    and no text operators, so this correctly yields nothing and flags OCR.
    """
    page_hint = data.count(b"/Type /Page") + data.count(b"/Type/Page")
    if b"/Encrypt" in data:
        return Extraction("", "pdf-encrypted", extractable=False, needs_ocr=False, page_hint=page_hint)

    pieces: list[str] = []
    for raw_stream in _PDF_STREAM.findall(data)[:200]:
        body = raw_stream
        if body[:2] in (b"\x78\x9c", b"\x78\x01", b"\x78\xda"):
            try:
                body = zlib.decompress(body)
            except zlib.error:
                try:
                    body = zlib.decompressobj().decompress(body)  # tolerate truncation
                except zlib.error:
                    continue
        if b"Tj" not in body and b"TJ" not in body:
            continue
        for literal in _PDF_TJ.findall(body):
            inner = literal[1:-1]
            inner = inner.replace(b"\\(", b"(").replace(b"\\)", b")").replace(b"\\\\", b"\\")
            pieces.append(inner.decode("latin-1", errors="replace"))

    text = _clean(" ".join(pieces))
    if text:
        return Extraction(text, "pdf-text-layer", extractable=True, needs_ocr=False, page_hint=page_hint)

    has_images = b"/Image" in data or b"/DCTDecode" in data or b"/JPXDecode" in data or b"/CCITTFaxDecode" in data
    return Extraction(
        "",
        "pdf-scanned-no-text-layer" if has_images else "pdf-no-recoverable-text",
        extractable=False,
        needs_ocr=True,
        page_hint=page_hint,
    )


def _extract_rtf(data: bytes) -> Extraction:
    text = _decode(data)
    text = re.sub(r"\\'[0-9a-fA-F]{2}", " ", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", text)
    text = text.replace("{", " ").replace("}", " ")
    cleaned = _clean(text)
    return Extraction(cleaned, "rtf-strip", extractable=bool(cleaned), needs_ocr=False)
