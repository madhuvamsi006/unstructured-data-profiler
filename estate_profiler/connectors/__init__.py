"""Estate source connectors."""

from __future__ import annotations

import os
from pathlib import Path

from .base import Connector, FileRef
from .local import LocalConnector


class SourceNotAllowed(PermissionError):
    """The requested source is outside the configured allowlist."""


def allowed_sources() -> list[str]:
    """Sources this deployment may scan. Empty means unrestricted.

    Set ``PROFILER_ALLOWED_SOURCES`` to a comma-separated list of prefixes
    (``gs://bucket/prefix`` or a local directory). This exists because the
    console lets a visitor type any source they like: unrestricted is correct
    on a laptop and dangerous on a public URL, where it would otherwise let
    anyone enumerate every bucket the runtime service account can read.
    """
    raw = os.environ.get("PROFILER_ALLOWED_SOURCES", "").strip()
    return [item.strip() for item in raw.split(",") if item.strip()]


def _upload_dir() -> str:
    """The server-managed upload scratch space, always implicitly allowed."""
    return os.environ.get("PROFILER_UPLOAD_DIR", "").strip()


def check_source_allowed(source_uri: str) -> None:
    """Raise SourceNotAllowed unless ``source_uri`` sits inside the allowlist."""
    allowed = list(allowed_sources())
    upload = _upload_dir()
    if upload:
        allowed.append(upload)
    if not allowed:
        return

    uri = _strip_quotes(source_uri)

    if uri.startswith("gs://"):
        target = uri.rstrip("/")
        for entry in allowed:
            if not entry.startswith("gs://"):
                continue
            prefix = entry.rstrip("/")
            # Match whole path segments so gs://data does not admit gs://data-prod.
            if target == prefix or target.startswith(prefix + "/"):
                return
        raise SourceNotAllowed(_denied_message(uri, allowed))

    # Local paths are resolved first so that ".." cannot climb out of an
    # allowed root.
    try:
        resolved = Path(uri).expanduser().resolve()
    except (OSError, ValueError):
        raise SourceNotAllowed(_denied_message(uri, allowed)) from None

    for entry in allowed:
        if entry.startswith("gs://"):
            continue
        try:
            root = Path(entry).expanduser().resolve()
        except (OSError, ValueError):
            continue
        if resolved == root or root in resolved.parents:
            return
    raise SourceNotAllowed(_denied_message(str(resolved), allowed))


def _denied_message(target: str, allowed: list[str]) -> str:
    return (
        f"This deployment is not permitted to scan {target}. "
        f"Allowed sources: {', '.join(allowed)}. "
        "Set PROFILER_ALLOWED_SOURCES to change this."
    )


def _strip_quotes(value: str) -> str:
    uri = value.strip()
    if len(uri) >= 2 and uri[0] == uri[-1] and uri[0] in ('"', "'"):
        return uri[1:-1].strip()
    return uri.strip('"').strip("'").strip()


def open_connector(source_uri: str, project: str | None = None) -> Connector:
    """Build a connector from a URI.

    Accepts ``gs://bucket/optional/prefix`` for Cloud Storage, and either a
    plain path, a ``file://`` URI, or a UNC path (``\\\\server\\share``) for
    local and mounted-share estates.
    """
    # Windows "Copy as path" wraps the path in double quotes, and people paste
    # that verbatim. An unstripped quote makes an absolute path look relative,
    # which then silently resolves against the working directory.
    uri = _strip_quotes(source_uri)

    # Enforced here because every caller - CLI, demo server, agent tool - opens
    # a connector, so there is no way to reach a source that skips this check.
    check_source_allowed(uri)

    if uri.startswith("gs://"):
        from .gcs import GCSConnector

        remainder = uri[len("gs://"):]
        bucket, _, prefix = remainder.partition("/")
        if not bucket:
            raise ValueError(f"Malformed GCS URI, expected gs://bucket[/prefix]: {source_uri}")
        return GCSConnector(bucket=bucket, prefix=prefix, project=project)

    if uri.startswith("file://"):
        from urllib.parse import unquote, urlparse

        parsed = urlparse(uri)
        path = unquote(parsed.path)
        # file://server/share -> UNC; file:///C:/x -> drive path with a leading slash.
        if parsed.netloc:
            path = f"//{parsed.netloc}{path}"
        elif len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return LocalConnector(path)

    if uri.startswith(("s3://", "https://", "http://", "sharepoint://")):
        raise ValueError(
            f"Source scheme not supported in v1: {source_uri}. "
            "Supported: gs://bucket[/prefix], a local path, or a mounted share path."
        )

    return LocalConnector(uri)


__all__ = [
    "Connector",
    "FileRef",
    "LocalConnector",
    "SourceNotAllowed",
    "allowed_sources",
    "check_source_allowed",
    "open_connector",
]
