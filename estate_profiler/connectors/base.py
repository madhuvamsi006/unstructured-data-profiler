"""Source-agnostic file reference and connector protocol.

A connector's only job is to enumerate files and hand back bytes on demand.
Everything downstream (hashing, PII, format health, scoring) works against
``FileRef`` and never knows whether the estate lives on a share or in GCS.
"""

from __future__ import annotations

import fnmatch
import posixpath
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Protocol, Sequence

# Directories that are noise in every estate scan: version control, OS cruft,
# build output, and caches. Counting them inflates file counts and dedupe stats.
DEFAULT_EXCLUDE_DIRS = frozenset(
    {
        ".git", ".svn", ".hg", "__pycache__", "node_modules", ".venv", "venv",
        ".idea", ".vscode", ".Trash", "$RECYCLE.BIN", "System Volume Information",
        ".DS_Store", "~snapshot", ".snapshot",
    }
)

# Files that are artifacts of the filesystem rather than business content.
DEFAULT_EXCLUDE_GLOBS = (
    "~$*",          # Office lock files
    ".~lock.*",     # LibreOffice lock files
    "Thumbs.db",
    "desktop.ini",
    ".DS_Store",
    "*.tmp",
    "*.part",
    "*.crdownload",
)


@dataclass
class FileRef:
    """One file in the estate, before any content has been read."""

    uri: str
    source: str              # "local" | "gcs"
    container: str           # root directory or bucket name
    rel_path: str            # path within the container, posix-style
    name: str
    ext: str                 # lowercase, no dot; "" when the file has none
    size_bytes: int
    modified: datetime | None = None
    declared_content_type: str | None = None
    depth: int = 0
    # Populated by the profiling passes.
    analysis: dict = field(default_factory=dict)

    @property
    def parent(self) -> str:
        parent = posixpath.dirname(self.rel_path)
        return parent or "/"

    def to_row(self) -> dict:
        row = asdict(self)
        row.pop("analysis", None)
        row["modified"] = self.modified.isoformat() if self.modified else None
        row.update(self.analysis)
        return row


class Connector(Protocol):
    """Minimal surface a source must implement to be profiled."""

    source: str
    container: str

    def walk(self, max_files: int) -> Iterator[FileRef]:
        """Yield up to ``max_files`` file references, cheapest metadata only."""

    def read(self, ref: FileRef, n_bytes: int) -> bytes:
        """Read the first ``n_bytes`` of the file. Returns b'' on failure."""


def should_skip_name(name: str, exclude_globs: Sequence[str] = DEFAULT_EXCLUDE_GLOBS) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in exclude_globs)


def split_ext(name: str) -> str:
    _, _, ext = name.rpartition(".")
    if ext == name or not ext:
        return ""
    # Guard against directories-with-dots style names producing absurd extensions.
    return ext.lower() if len(ext) <= 12 and ext.isalnum() else ""


def utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
