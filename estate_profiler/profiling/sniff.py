"""Magic-byte content sniffing.

Extensions lie — renamed files, stripped extensions, and ``.pdf`` files that are
actually ZIPs are routine in a real estate. Every downstream decision uses the
sniffed type, not the declared one, and the disagreement is itself a finding.
"""

from __future__ import annotations

import zipfile
from io import BytesIO

# (offset, signature, sniffed_type). Order matters: the first match wins, so
# more specific signatures must precede the containers they are built on.
_SIGNATURES: tuple[tuple[int, bytes, str], ...] = (
    (0, b"%PDF-", "pdf"),
    (0, b"\x89PNG\r\n\x1a\n", "png"),
    (0, b"\xff\xd8\xff", "jpeg"),
    (0, b"GIF87a", "gif"),
    (0, b"GIF89a", "gif"),
    (0, b"BM", "bmp"),
    (0, b"II*\x00", "tiff"),
    (0, b"MM\x00*", "tiff"),
    (0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole2"),   # legacy doc/xls/ppt/msg
    (0, b"PK\x03\x04", "zip"),                            # ooxml/odf/jar/plain zip
    (0, b"PK\x05\x06", "zip-empty"),
    (0, b"Rar!\x1a\x07", "rar"),
    (0, b"7z\xbc\xaf\x27\x1c", "7z"),
    (0, b"\x1f\x8b", "gzip"),
    (0, b"{\\rtf", "rtf"),
    (0, b"SQLite format 3\x00", "sqlite"),
    (0, b"\x7fELF", "elf"),
    (0, b"MZ", "pe"),
    (0, b"-----BEGIN ", "pem"),
    (0, b"\xff\xfe", "utf16-text"),
    (0, b"\xfe\xff", "utf16-text"),
    (0, b"\xef\xbb\xbf", "utf8-text"),
    (0, b"OggS", "ogg"),
    (0, b"fLaC", "flac"),
    (0, b"ID3", "mp3"),
    (0, b"\x00\x00\x01\xba", "mpeg"),
    (4, b"ftyp", "mp4"),
    (0, b"RIFF", "riff"),                                 # wav/avi/webp
    (0, b"\x25\x21PS", "postscript"),
    (0, b"\x1aE\xdf\xa3", "matroska"),
)

# What the extension claims the content should sniff as. A file is only flagged
# as mismatched when its extension appears here and the sniff disagrees.
_EXT_EXPECTS: dict[str, frozenset[str]] = {
    "pdf": frozenset({"pdf"}),
    "docx": frozenset({"ooxml-word", "zip"}),
    "xlsx": frozenset({"ooxml-excel", "zip"}),
    "pptx": frozenset({"ooxml-ppt", "zip"}),
    "docm": frozenset({"ooxml-word", "zip"}),
    "xlsm": frozenset({"ooxml-excel", "zip"}),
    "doc": frozenset({"ole2"}),
    "xls": frozenset({"ole2"}),
    "ppt": frozenset({"ole2"}),
    "msg": frozenset({"ole2"}),
    "odt": frozenset({"odf-text", "zip"}),
    "ods": frozenset({"odf-sheet", "zip"}),
    "odp": frozenset({"odf-presentation", "zip"}),
    "zip": frozenset({"zip", "zip-empty"}),
    "rar": frozenset({"rar"}),
    "7z": frozenset({"7z"}),
    "gz": frozenset({"gzip"}),
    "tgz": frozenset({"gzip"}),
    "png": frozenset({"png"}),
    "jpg": frozenset({"jpeg"}),
    "jpeg": frozenset({"jpeg"}),
    "gif": frozenset({"gif"}),
    "bmp": frozenset({"bmp"}),
    "tif": frozenset({"tiff"}),
    "tiff": frozenset({"tiff"}),
    "rtf": frozenset({"rtf"}),
    "mp4": frozenset({"mp4"}),
    "mp3": frozenset({"mp3", "riff"}),
    "exe": frozenset({"pe"}),
    "dll": frozenset({"pe"}),
}

# OOXML and ODF are both ZIPs; the member list tells them apart.
_ZIP_MARKERS: tuple[tuple[str, str], ...] = (
    ("word/document.xml", "ooxml-word"),
    ("xl/workbook.xml", "ooxml-excel"),
    ("ppt/presentation.xml", "ooxml-ppt"),
    ("EncryptedPackage", "ooxml-encrypted"),
)

_TEXTUAL_EXTS = frozenset(
    {
        "txt", "md", "markdown", "csv", "tsv", "json", "jsonl", "ndjson", "xml",
        "html", "htm", "yaml", "yml", "log", "sql", "py", "js", "ts", "java",
        "go", "rb", "c", "h", "cpp", "cs", "sh", "ps1", "ini", "cfg", "conf",
        "toml", "env", "properties", "rst", "tex", "vtt", "srt",
    }
)


def _looks_textual(head: bytes) -> bool:
    """Heuristic: mostly printable, no NUL bytes in the first chunk."""
    if not head:
        return False
    sample = head[:4096]
    if b"\x00" in sample:
        return False
    printable = sum(1 for b in sample if 32 <= b < 127 or b in (9, 10, 13))
    return printable / len(sample) > 0.85


def sniff(head: bytes, ext: str = "") -> str:
    """Return a canonical content type derived from bytes, falling back to text/unknown."""
    if not head:
        return "empty"

    for offset, signature, kind in _SIGNATURES:
        if head[offset : offset + len(signature)] == signature:
            if kind == "zip":
                return _refine_zip(head)
            if kind == "riff":
                return _refine_riff(head)
            if kind in ("utf16-text", "utf8-text"):
                return "text"
            return kind

    if ext in _TEXTUAL_EXTS and _looks_textual(head):
        return "text"
    if _looks_textual(head):
        return "text"
    return "unknown-binary"


def _refine_zip(head: bytes) -> str:
    """Distinguish OOXML / ODF / plain ZIP by peeking at the archive members."""
    try:
        with zipfile.ZipFile(BytesIO(head)) as archive:
            names = archive.namelist()
    except Exception:
        # A truncated sample of a large ZIP is normal: fall back to the local
        # file header, which names the first member even in a partial read.
        text = head[:8192]
        for marker, kind in _ZIP_MARKERS:
            if marker.encode() in text:
                return kind
        if b"mimetypeapplication/vnd.oasis.opendocument.text" in text:
            return "odf-text"
        if b"mimetypeapplication/vnd.oasis.opendocument.spreadsheet" in text:
            return "odf-sheet"
        if b"mimetypeapplication/vnd.oasis.opendocument.presentation" in text:
            return "odf-presentation"
        return "zip"

    joined = "\n".join(names)
    for marker, kind in _ZIP_MARKERS:
        if marker in joined:
            return kind
    if "mimetype" in names:
        return "odf"
    return "zip"


def _refine_riff(head: bytes) -> str:
    tag = head[8:12]
    return {b"WEBP": "webp", b"WAVE": "wav", b"AVI ": "avi"}.get(tag, "riff")


def extension_matches(ext: str, sniffed: str) -> bool | None:
    """True/False when the extension makes a checkable claim, None when it does not."""
    expected = _EXT_EXPECTS.get(ext)
    if expected is None:
        return None
    return sniffed in expected
