"""Runtime configuration for the Unstructured Data Profiler agent."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Load the project's .env.

    ``adk web`` and ``adk run`` do this themselves, but the CLI and the demo
    server are ordinary Python entry points — without this they would silently
    see a different configuration than the agent does, which is a confusing way
    to lose an afternoon.
    """
    env_file = _PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_file, override=False)
    except ImportError:
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    """Everything the profiler needs that can differ between engagements."""

    project_id: str = field(default_factory=lambda: os.environ.get("GOOGLE_CLOUD_PROJECT", ""))
    location: str = field(default_factory=lambda: os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"))
    model: str = field(default_factory=lambda: os.environ.get("PROFILER_MODEL", "gemini-2.5-flash"))

    # Where run artifacts (manifests, reports, dashboards) are written. Anchored
    # to the project rather than the working directory, so `adk web`, the CLI
    # and the demo server all read and write the same run history.
    run_root: Path = field(
        default_factory=lambda: (_PROJECT_ROOT / os.environ.get("PROFILER_RUN_ROOT", "runs")).resolve()
    )

    # Crawl limits. An estate triage is a sample, not a full read of every byte.
    max_files: int = field(default_factory=lambda: _env_int("PROFILER_MAX_FILES", 25_000))
    # Bytes read per file for content hashing / text extraction / PII sampling.
    sample_bytes: int = field(default_factory=lambda: _env_int("PROFILER_SAMPLE_BYTES", 1_048_576))
    # Files at or below this size are hashed in full for exact-duplicate detection.
    full_hash_max_bytes: int = field(
        default_factory=lambda: _env_int("PROFILER_FULL_HASH_MAX_BYTES", 33_554_432)
    )

    # Cloud DLP escalation is opt-in: it costs money and needs the API enabled.
    use_dlp: bool = field(default_factory=lambda: _env_bool("PROFILER_USE_DLP", False))
    # Only the N riskiest files (by local heuristic score) are sent to DLP.
    dlp_sample_limit: int = field(default_factory=lambda: _env_int("PROFILER_DLP_SAMPLE_LIMIT", 100))

    bq_dataset: str = field(default_factory=lambda: os.environ.get("PROFILER_BQ_DATASET", "estate_profiler"))
    bq_table: str = field(default_factory=lambda: os.environ.get("PROFILER_BQ_TABLE", "file_inventory"))

    def run_dir(self, run_id: str) -> Path:
        return self.run_root / run_id


SETTINGS = Settings()
