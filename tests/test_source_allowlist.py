"""Source allowlist tests.

This is the control that makes a publicly reachable deployment acceptable:
without it, anyone who opens the console could type any bucket name and have
the runtime service account enumerate it. These tests are therefore about
what must NOT be reachable, not just what should be.

Run with: python tests/test_source_allowlist.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from estate_profiler.connectors import (  # noqa: E402
    SourceNotAllowed,
    allowed_sources,
    check_source_allowed,
    open_connector,
)

_TMP = Path(tempfile.mkdtemp(prefix="allowlist-"))
ALLOWED_DIR = _TMP / "public_estate"
NESTED = ALLOWED_DIR / "team" / "docs"
SECRET_DIR = _TMP / "private_stuff"
for directory in (NESTED, SECRET_DIR):
    directory.mkdir(parents=True, exist_ok=True)
(ALLOWED_DIR / "a.txt").write_text("hello")
(SECRET_DIR / "secret.txt").write_text("nope")


def _set(value: str | None) -> None:
    if value is None:
        os.environ.pop("PROFILER_ALLOWED_SOURCES", None)
    else:
        os.environ["PROFILER_ALLOWED_SOURCES"] = value


def _denied(source: str) -> bool:
    try:
        check_source_allowed(source)
        return False
    except SourceNotAllowed:
        return True


# --------------------------------------------------------------------------

def test_empty_allowlist_permits_everything():
    """A laptop run must stay unrestricted - this only engages when configured."""
    _set(None)
    assert allowed_sources() == []
    check_source_allowed("gs://any-bucket-at-all")
    check_source_allowed(str(SECRET_DIR))


def test_allowed_directory_and_its_children_pass():
    _set(str(ALLOWED_DIR))
    check_source_allowed(str(ALLOWED_DIR))
    check_source_allowed(str(NESTED))
    check_source_allowed(str(ALLOWED_DIR / "a.txt"))


def test_sibling_directory_is_denied():
    _set(str(ALLOWED_DIR))
    assert _denied(str(SECRET_DIR))


def test_parent_directory_is_denied():
    _set(str(NESTED))
    assert _denied(str(ALLOWED_DIR))


def test_dot_dot_cannot_escape_an_allowed_root():
    """The obvious attack: climb out of the allowed directory."""
    _set(str(ALLOWED_DIR))
    for escape in (
        str(ALLOWED_DIR / ".." / "private_stuff"),
        str(ALLOWED_DIR / ".." / ".."),
        str(NESTED / ".." / ".." / ".." / "private_stuff"),
    ):
        assert _denied(escape), f"escaped the allowlist via {escape}"


def test_prefix_lookalike_bucket_is_denied():
    """gs://data must not admit gs://data-prod - a whole-segment match only."""
    _set("gs://data")
    check_source_allowed("gs://data")
    check_source_allowed("gs://data/sub/path")
    assert _denied("gs://data-prod")
    assert _denied("gs://database-backups")


def test_gcs_prefix_scoping():
    _set("gs://client-estate/public")
    check_source_allowed("gs://client-estate/public")
    check_source_allowed("gs://client-estate/public/2024")
    assert _denied("gs://client-estate")
    assert _denied("gs://client-estate/private")


def test_local_allowlist_does_not_admit_gcs():
    _set(str(ALLOWED_DIR))
    assert _denied("gs://some-bucket")


def test_gcs_allowlist_does_not_admit_local():
    _set("gs://ok-bucket")
    assert _denied(str(ALLOWED_DIR))


def test_multiple_entries_are_all_honoured():
    _set(f"{ALLOWED_DIR},gs://ok-bucket")
    check_source_allowed(str(ALLOWED_DIR))
    check_source_allowed("gs://ok-bucket/x")
    assert _denied(str(SECRET_DIR))
    assert _denied("gs://other-bucket")


def test_quoted_input_is_still_checked():
    """Quote stripping must not become a way around the allowlist."""
    _set(str(ALLOWED_DIR))
    assert _denied(f'"{SECRET_DIR}"')
    assert _denied(f'"{SECRET_DIR}')
    check_source_allowed(f'"{ALLOWED_DIR}"')


def test_open_connector_enforces_the_allowlist():
    """Every caller goes through open_connector, so the check must live there."""
    _set(str(ALLOWED_DIR))
    connector = open_connector(str(ALLOWED_DIR))
    assert connector is not None
    try:
        open_connector(str(SECRET_DIR))
    except SourceNotAllowed:
        pass
    else:
        raise AssertionError("open_connector bypassed the allowlist")


def test_denial_message_names_what_is_allowed():
    _set("gs://ok-bucket")
    try:
        check_source_allowed("gs://other")
    except SourceNotAllowed as exc:
        message = str(exc)
        assert "gs://ok-bucket" in message
        assert "PROFILER_ALLOWED_SOURCES" in message
    else:
        raise AssertionError("expected SourceNotAllowed")


if __name__ == "__main__":
    import shutil
    import traceback

    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failures = 0
    original = os.environ.get("PROFILER_ALLOWED_SOURCES")
    try:
        for name, fn in tests:
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception:
                failures += 1
                print(f"  FAIL  {name}")
                traceback.print_exc()
    finally:
        _set(original)
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
