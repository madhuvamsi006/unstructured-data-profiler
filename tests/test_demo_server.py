"""Demo server tests — starts the real server on a free port and drives it.

Covers everything the demo console depends on except the model call itself,
which needs credentials and costs money; that lives in smoke_live_agent.py.

Run with: python tests/test_demo_server.py
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    """Find a bindable port in a fixed low range.

    Deliberately not ``bind(port 0)``: Windows reserves large swathes of the
    ephemeral range for Hyper-V/WSL (see ``netsh int ipv4 show
    excludedportrange``), and a port handed out there can fail to bind a moment
    later. Scanning a quiet fixed range is boring and reliable.
    """
    for port in range(8700, 8800):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError("no free port in 8700-8799")


PORT = _free_port()
BASE = f"http://127.0.0.1:{PORT}"
_server: subprocess.Popen | None = None


import tempfile

# Kept out of runs/ on purpose: that directory is scanned as run history, and
# test scratch files have no business showing up there.
_LOG = Path(tempfile.gettempdir()) / "estate_profiler_test_server.log"


def start_server() -> None:
    """Launch the demo server as a subprocess and wait for it to answer."""
    global _server
    import os

    env = dict(os.environ, PORT=str(PORT), HOST="127.0.0.1")
    _LOG.parent.mkdir(parents=True, exist_ok=True)
    # Log to a file rather than PIPE: a full pipe buffer deadlocks the child.
    log = open(_LOG, "wb")
    _server = subprocess.Popen(
        [sys.executable, "demo/server.py"],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 90
    last_error: Exception | None = None
    while time.time() < deadline:
        if _server.poll() is not None:
            raise RuntimeError(
                "server exited early:\n" + _LOG.read_text(errors="replace")[-2000:]
            )
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=5) as response:
                if response.status == 200:
                    return
                last_error = RuntimeError(f"health returned {response.status}")
        except Exception as exc:
            last_error = exc
        time.sleep(0.4)
    raise RuntimeError(
        f"server did not become ready within 90s.\n"
        f"last error: {type(last_error).__name__}: {last_error}\n"
        f"child log:\n{_LOG.read_text(errors='replace')[-2000:]}"
    )


def stop_server() -> None:
    if _server and _server.poll() is None:
        _server.terminate()
        try:
            _server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _server.kill()


def get_json(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=60) as response:
        return json.loads(response.read())


def sse(path: str, timeout: int = 180) -> list[tuple[str, dict]]:
    """Consume a server-sent-event stream into (event, payload) pairs."""
    events: list[tuple[str, dict]] = []
    with urllib.request.urlopen(BASE + path, timeout=timeout) as response:
        current = None
        for raw in response:
            line = raw.decode("utf-8").rstrip()
            if line.startswith("event: "):
                current = line[7:]
            elif line.startswith("data: ") and current:
                events.append((current, json.loads(line[6:])))
    return events


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------

def test_health_reports_model_and_auth_state():
    health = get_json("/api/health")
    assert health["model"]
    assert "auth" in health and "ok" in health["auth"]
    # When auth is down the UI needs an actionable fix, not just a failure.
    if not health["auth"]["ok"]:
        assert health["auth"].get("fix"), "unauthenticated health must carry a fix"


def test_index_and_static_assets_are_served():
    for path in ("/", "/static/app.js", "/static/styles.css"):
        with urllib.request.urlopen(BASE + path, timeout=30) as response:
            assert response.status == 200
            assert len(response.read()) > 500, path


def test_scan_streams_progress_then_done():
    query = urllib.parse.urlencode({"source": "sample_estate", "max_files": 0})
    events = sse("/api/scan?" + query)
    kinds = [kind for kind, _ in events]
    assert kinds[0] == "started"
    assert "done" in kinds, kinds
    assert "error" not in kinds, events

    summary = dict(events[-1][1])
    assert summary["inventory"]["total_files"] > 0
    assert summary["scan"]["dlp_used"] is False      # DLP must stay opt-in
    return summary["run_id"]


def test_scan_rejects_a_bad_source_without_crashing():
    query = urllib.parse.urlencode({"source": "./definitely-not-a-real-estate"})
    events = sse("/api/scan?" + query, timeout=60)
    kinds = [kind for kind, _ in events]
    assert "error" in kinds, kinds
    assert "done" not in kinds
    message = next(payload["message"] for kind, payload in events if kind == "error")
    assert message and "Traceback" not in message


def test_scan_requires_a_source():
    try:
        urllib.request.urlopen(BASE + "/api/scan?source=%20", timeout=30)
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
    else:
        raise AssertionError("expected HTTP 400")


def test_run_artifacts_are_all_downloadable():
    run_id = test_scan_streams_progress_then_done()
    expected = {
        "/summary": "application/json",
        "/dashboard": "text/html",
        "/report": "text/markdown",
        "/manifest": "application/x-ndjson",
    }
    for path, content_type in expected.items():
        with urllib.request.urlopen(f"{BASE}/api/runs/{run_id}{path}", timeout=60) as response:
            assert response.status == 200
            assert content_type in response.headers.get("content-type", ""), path
            assert len(response.read()) > 200, path


def test_served_dashboard_is_a_complete_document():
    runs = get_json("/api/runs")["runs"]
    assert runs, "no runs to render"
    with urllib.request.urlopen(f"{BASE}/api/runs/{runs[0]['run_id']}/dashboard", timeout=60) as response:
        html = response.read().decode("utf-8")
    # Served standalone it needs the skeleton the Artifact host would supply.
    assert html.lstrip().startswith("<!doctype html>")
    assert "<title>" in html
    assert "__DATA__" not in html and "__TITLE__" not in html


def test_dashboard_never_contains_sensitive_values():
    runs = get_json("/api/runs")["runs"]
    with urllib.request.urlopen(f"{BASE}/api/runs/{runs[0]['run_id']}/dashboard", timeout=60) as response:
        html = response.read().decode("utf-8")
    for planted in ("AKIAIOSFODNN7EXAMPLE", "123-45-6789", "4111 1111",
                    "GB82WEST12345698765432", "hunter2demo", "BEGIN PRIVATE KEY"):
        assert planted not in html, f"dashboard leaked {planted}"


def test_unknown_run_is_a_404():
    for path in ("/summary", "/dashboard", "/report", "/manifest"):
        try:
            urllib.request.urlopen(BASE + "/api/runs/no-such-run" + path, timeout=30)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404, path
        else:
            raise AssertionError(f"expected 404 for {path}")


def test_runs_listing_includes_the_latest_scan():
    runs = get_json("/api/runs")["runs"]
    assert runs
    for run in runs:
        assert run["run_id"] and run["total_files"] is not None


def test_chat_rejects_an_empty_message():
    request = urllib.request.Request(
        BASE + "/api/chat",
        data=json.dumps({"message": "   "}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
    else:
        raise AssertionError("expected HTTP 400")


def test_chat_degrades_with_a_fix_when_auth_is_down():
    """Without credentials the console must stay usable and say how to fix it."""
    if get_json("/api/health")["auth"]["ok"]:
        return  # authenticated: covered by smoke_live_agent.py instead
    request = urllib.request.Request(
        BASE + "/api/chat",
        data=json.dumps({"message": "hello"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read().decode("utf-8")
    assert "event: error" in body
    assert "gcloud auth application-default login" in body or "GOOGLE_API_KEY" in body


if __name__ == "__main__":
    import traceback

    if not (ROOT / "sample_estate").exists():
        print("Building the sample estate first ...")
        subprocess.run(
            [sys.executable, "tools/make_sample_estate.py", "sample_estate"],
            cwd=str(ROOT), check=True,
        )

    print(f"Starting demo server on port {PORT} ...")
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failures = 0
    # The try must wrap start_server too: a startup failure used to leave the
    # child running, and those orphans then interfered with the next run.
    try:
        start_server()
        for name, fn in tests:
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception:
                failures += 1
                print(f"  FAIL  {name}")
                traceback.print_exc()
    finally:
        stop_server()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
