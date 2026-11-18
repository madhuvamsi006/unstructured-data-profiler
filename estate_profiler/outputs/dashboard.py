"""Self-contained HTML dashboard for an estate triage run.

The summary is embedded as JSON and rendered client-side with hand-drawn SVG —
no chart library, so the file opens from disk, from a bucket, or as a published
Artifact with nothing to install and nothing to fetch.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import report

_TEMPLATE_PATH = Path(__file__).parent / "dashboard_template.html"


def build_html(summary: dict, steps: list[str] | None = None) -> str:
    payload = {
        "summary": summary,
        "steps": steps if steps is not None else report.next_steps(summary, plain=True),
    }
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    # </script> inside embedded JSON would close the host script tag early.
    encoded = json.dumps(payload, ensure_ascii=False, default=str).replace("</", "<\\/")
    return template.replace("__DATA__", encoded).replace("__TITLE__", _escape(_short_name(summary)))


def _short_name(summary: dict) -> str:
    """A human-sized name for the estate — the tab title, not the full path."""
    container = str(summary.get("container") or summary.get("source_uri") or "Estate")
    leaf = container.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return leaf or container


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def write_dashboard(summary: dict, out_path: Path, steps: list[str] | None = None) -> str:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(build_html(summary, steps), encoding="utf-8")
    return str(out_path)
