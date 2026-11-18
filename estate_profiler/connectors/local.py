"""Local filesystem / mounted SMB share connector.

A mapped network drive or a UNC path looks like an ordinary directory tree to
Python, so this one connector covers both laptop folders and file shares.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Iterator

from .base import (
    DEFAULT_EXCLUDE_DIRS,
    DEFAULT_EXCLUDE_GLOBS,
    FileRef,
    should_skip_name,
    split_ext,
)


class LocalConnector:
    source = "local"

    def __init__(self, root: str, exclude_dirs: set[str] | None = None):
        self.root_path = Path(root).expanduser().resolve()
        if not self.root_path.exists():
            raise FileNotFoundError(f"Estate root does not exist: {self.root_path}")

        # Pointing at a single file is a reasonable thing to do — "profile this
        # one document" is how people try the tool out. Treat it as an estate
        # of one rather than refusing.
        self.single_file = self.root_path.is_file()
        self.base_dir = self.root_path.parent if self.single_file else self.root_path
        self.container = str(self.base_dir)
        self.exclude_dirs = set(exclude_dirs) if exclude_dirs else set(DEFAULT_EXCLUDE_DIRS)

    def _ref_for(self, full: str, rel: str) -> FileRef | None:
        try:
            stat = os.stat(full)
        except OSError:
            return None
        name = os.path.basename(full)
        return FileRef(
            uri=Path(full).as_uri(),
            source=self.source,
            container=self.container,
            rel_path=rel,
            name=name,
            ext=split_ext(name),
            size_bytes=stat.st_size,
            modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            depth=rel.count("/"),
        )

    def walk(self, max_files: int) -> Iterator[FileRef]:
        if self.single_file:
            ref = self._ref_for(str(self.root_path), self.root_path.name)
            if ref is not None:
                yield ref
            return

        count = 0
        root_str = str(self.root_path)
        for dirpath, dirnames, filenames in os.walk(root_str, onerror=lambda _: None):
            # Prune in place so os.walk never descends into excluded trees.
            dirnames[:] = [d for d in dirnames if d not in self.exclude_dirs and not d.startswith("$")]
            for filename in filenames:
                if count >= max_files:
                    return
                if should_skip_name(filename, DEFAULT_EXCLUDE_GLOBS):
                    continue
                full = os.path.join(dirpath, filename)
                rel = PurePath(os.path.relpath(full, root_str)).as_posix()
                ref = self._ref_for(full, rel)
                if ref is None:
                    continue
                yield ref
                count += 1

    def read(self, ref: FileRef, n_bytes: int) -> bytes:
        full = self.base_dir / PurePath(ref.rel_path)
        try:
            with open(full, "rb") as handle:
                return handle.read(n_bytes)
        except OSError:
            return b""
