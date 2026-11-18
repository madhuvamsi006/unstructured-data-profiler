"""Demo server: a presentable front end for the Unstructured Data Profiler.

Wraps two things the agent already does and makes them watchable:

  * the profiling engine — run a scan, stream progress, show the dashboard
  * the ADK agent itself — ask questions about the scan in natural language

Run with:  python demo/server.py        (then open http://127.0.0.1:8080)
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import shutil
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from estate_profiler.config import SETTINGS  # noqa: E402
from estate_profiler.engine import RunStore, scan_estate  # noqa: E402
from estate_profiler.outputs import dashboard as dashboard_output  # noqa: E402
from estate_profiler.outputs import report as report_output  # noqa: E402

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Unstructured Data Profiler — Demo")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# Auth / readiness
# ---------------------------------------------------------------------------


_auth_cache: tuple[float, dict] | None = None
_AUTH_TTL_SECONDS = 30.0


def _auth_status() -> dict:
    """Report whether the agent can actually reach a model, and how to fix it.

    Cached briefly: the check refreshes credentials over the network, and when
    credentials are broken that call is slow to fail. Without the cache the
    console stalls for seconds on load — on exactly the laptop where auth is
    already broken, which is the worst possible moment.

    The demo is useful even without a model — scanning, the dashboard and the
    report all work offline — so the UI degrades rather than blocking.
    """
    global _auth_cache
    now = time.monotonic()
    if _auth_cache and now - _auth_cache[0] < _AUTH_TTL_SECONDS:
        return _auth_cache[1]
    status = _compute_auth_status()
    _auth_cache = (now, status)
    return status


def _compute_auth_status() -> dict:
    import os

    if os.environ.get("GOOGLE_API_KEY"):
        return {"ok": True, "mode": "api_key", "detail": "Using GOOGLE_API_KEY (AI Studio)."}

    use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "TRUE").upper() in ("1", "TRUE", "YES")
    if not use_vertex:
        return {
            "ok": False,
            "mode": "none",
            "detail": "No GOOGLE_API_KEY set and Vertex AI is disabled.",
            "fix": "Set GOOGLE_API_KEY, or set GOOGLE_GENAI_USE_VERTEXAI=TRUE and run: gcloud auth application-default login",
        }

    if not SETTINGS.project_id:
        return {
            "ok": False,
            "mode": "vertex",
            "detail": "Vertex AI selected but no project is configured.",
            "fix": "Set GOOGLE_CLOUD_PROJECT in your .env file.",
        }

    try:
        import google.auth
        import google.auth.transport.requests

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        credentials.refresh(google.auth.transport.requests.Request())
    except Exception as exc:
        return {
            "ok": False,
            "mode": "vertex",
            "detail": f"Application Default Credentials are not usable: {type(exc).__name__}.",
            "fix": "Run: gcloud auth application-default login",
        }

    return {
        "ok": True,
        "mode": "vertex",
        "detail": f"Vertex AI on project {SETTINGS.project_id} ({SETTINGS.location}).",
    }


@app.get("/api/health")
def health() -> dict:
    import os

    auth = _auth_status()
    sample = ROOT / "sample_estate"
    # Cloud Run sets K_SERVICE. Hosted, there is no file share to point at, so
    # the UI offers the bundled sample and GCS rather than local paths.
    hosted = bool(os.environ.get("K_SERVICE"))
    from estate_profiler.connectors import allowed_sources

    return {
        "model": SETTINGS.model,
        "project": SETTINGS.project_id or None,
        "location": SETTINGS.location,
        "auth": auth,
        "dlp_configured": bool(SETTINGS.project_id),
        "sample_estate": str(sample) if sample.exists() else None,
        "run_root": str(SETTINGS.run_root),
        "environment": "cloud_run" if hosted else "local",
        "revision": os.environ.get("K_REVISION"),
        "ephemeral_runs": hosted,
        "allowed_sources": allowed_sources(),
        "defaults": {"max_files": SETTINGS.max_files, "sample_bytes": SETTINGS.sample_bytes},
    }


# ---------------------------------------------------------------------------
# File upload
# ---------------------------------------------------------------------------

_MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB guard


@app.post("/api/upload")
async def upload_file(file: UploadFile):
    """Accept a file upload and store it in the server-side scratch space.

    Returns the saved path so the caller can pass it straight to /api/scan.
    The upload dir is always allowed by the profiler's source allowlist so
    users can scan their own documents even on a locked-down Cloud Run instance.
    """
    upload_dir = Path(os.environ.get("PROFILER_UPLOAD_DIR", "/tmp/uploads"))
    upload_dir.mkdir(parents=True, exist_ok=True)

    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided.")

    # Sanitise: take only the basename so path traversal is impossible.
    safe_name = Path(file.filename).name
    dest = upload_dir / safe_name
    if dest.exists():
        stem = Path(safe_name).stem
        suffix = Path(safe_name).suffix
        dest = upload_dir / f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"

    try:
        with open(dest, "wb") as fh:
            read_bytes = 0
            while True:
                chunk = await file.read(65536)
                if not chunk:
                    break
                read_bytes += len(chunk)
                if read_bytes > _MAX_UPLOAD_BYTES:
                    dest.unlink(missing_ok=True)
                    raise HTTPException(status_code=413, detail="File exceeds the 50 MB upload limit.")
                fh.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Upload failed: {exc}") from exc

    return {"path": str(dest), "filename": dest.name, "size_bytes": dest.stat().st_size}


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@app.get("/api/scan")
async def scan(source: str, max_files: int = 0, sample_bytes: int = 0, use_dlp: bool = False):
    """Run a scan, streaming progress as server-sent events.

    The engine is synchronous and CPU-bound, so it runs on a worker thread and
    pushes progress through a queue; the event loop stays free to flush the
    stream, which is what makes the crawl visible rather than a frozen spinner.
    """
    if not source.strip():
        raise HTTPException(status_code=400, detail="A source is required.")

    updates: queue.Queue = queue.Queue()

    def on_progress(seen: int, limit: int) -> None:
        updates.put(("progress", {"seen": seen, "limit": limit}))

    def worker() -> None:
        try:
            summary = scan_estate(
                source,
                settings=SETTINGS,
                max_files=max_files or None,
                sample_bytes=sample_bytes or None,
                use_dlp=use_dlp,
                progress=on_progress,
            )
            store = RunStore(summary["run_id"], SETTINGS)
            report_output.write_reports(summary, store.dir)
            dashboard_output.write_dashboard(summary, store.dir / "dashboard.html")
            updates.put(("done", summary))
        except (FileNotFoundError, NotADirectoryError, ValueError, PermissionError) as exc:
            updates.put(("error", {"message": str(exc)}))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            updates.put(("error", {"message": f"{type(exc).__name__}: {exc}"}))
        finally:
            updates.put((None, None))

    async def stream() -> AsyncIterator[str]:
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        yield _sse("started", {"source": source})
        loop = asyncio.get_running_loop()
        while True:
            kind, payload = await loop.run_in_executor(None, updates.get)
            if kind is None:
                break
            yield _sse(kind, payload)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/runs")
def list_runs() -> dict:
    root = SETTINGS.run_root
    if not root.exists():
        return {"runs": []}
    runs = []
    for directory in sorted(root.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True)[:25]:
        summary_file = directory / "summary.json"
        if not summary_file.exists():
            continue
        try:
            summary = json.loads(summary_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        runs.append(
            {
                "run_id": summary.get("run_id", directory.name),
                "source_uri": summary.get("source_uri"),
                "generated_at": summary.get("generated_at"),
                "total_files": summary.get("inventory", {}).get("total_files"),
                "mean_readiness": summary.get("readiness", {}).get("mean_score"),
            }
        )
    return {"runs": runs}


@app.get("/api/runs/{run_id}/summary")
def run_summary(run_id: str) -> JSONResponse:
    summary = RunStore(run_id, SETTINGS).read_summary()
    if not summary:
        raise HTTPException(status_code=404, detail=f"No such run: {run_id}")
    return JSONResponse(summary)


@app.get("/api/runs/{run_id}/dashboard", response_class=HTMLResponse)
def run_dashboard(run_id: str) -> HTMLResponse:
    store = RunStore(run_id, SETTINGS)
    summary = store.read_summary()
    if not summary:
        raise HTTPException(status_code=404, detail=f"No such run: {run_id}")
    # The dashboard template is authored for the Artifact host, which supplies
    # the document skeleton; standalone serving has to add it back.
    body = dashboard_output.build_html(summary)
    return HTMLResponse(
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<style>body{margin:0}</style>" + body + "</body></html>"
    )


@app.get("/api/runs/{run_id}/report")
def run_report(run_id: str) -> FileResponse:
    path = RunStore(run_id, SETTINGS).dir / "report.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="No report for that run.")
    return FileResponse(path, media_type="text/markdown", filename=f"estate-report-{run_id}.md")


@app.get("/api/runs/{run_id}/manifest")
def run_manifest(run_id: str) -> FileResponse:
    path = RunStore(run_id, SETTINGS).manifest_path
    if not path.exists():
        raise HTTPException(status_code=404, detail="No manifest for that run.")
    return FileResponse(path, media_type="application/x-ndjson", filename=f"manifest-{run_id}.jsonl")


# ---------------------------------------------------------------------------
# Agent chat
# ---------------------------------------------------------------------------

_APP_NAME = "estate_profiler_demo"
_runner = None
_runner_lock = threading.Lock()


def _get_runner():
    global _runner
    with _runner_lock:
        if _runner is None:
            from google.adk.runners import InMemoryRunner

            from estate_profiler.agent import root_agent

            _runner = InMemoryRunner(agent=root_agent, app_name=_APP_NAME)
        return _runner


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    run_id: str | None = None


@app.post("/api/chat")
async def chat(request: ChatRequest):
    """Send a message to the agent and stream its thinking, tool calls and reply."""
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message is required.")

    auth = _auth_status()
    if not auth["ok"]:
        async def refuse() -> AsyncIterator[str]:
            yield _sse("error", {
                "message": auth["detail"],
                "fix": auth.get("fix", ""),
            })
        return StreamingResponse(refuse(), media_type="text/event-stream")

    from google.genai import types

    runner = _get_runner()
    user_id = "demo-user"

    async def stream() -> AsyncIterator[str]:
        try:
            session_id = request.session_id
            if session_id:
                existing = await runner.session_service.get_session(
                    app_name=_APP_NAME, user_id=user_id, session_id=session_id
                )
                if existing is None:
                    session_id = None
            if not session_id:
                # Seed the session with the current run so the agent can answer
                # about it without the user repeating the run id every turn.
                state = {"estate_run_id": request.run_id} if request.run_id else {}
                session = await runner.session_service.create_session(
                    app_name=_APP_NAME, user_id=user_id, state=state
                )
                session_id = session.id
            yield _sse("session", {"session_id": session_id})

            message = types.Content(role="user", parts=[types.Part(text=request.message)])
            async for event in runner.run_async(
                user_id=user_id, session_id=session_id, new_message=message
            ):
                content = getattr(event, "content", None)
                for part in (getattr(content, "parts", None) or []):
                    call = getattr(part, "function_call", None)
                    if call is not None:
                        yield _sse("tool_call", {
                            "name": call.name,
                            "args": {k: v for k, v in (call.args or {}).items()},
                        })
                        continue
                    response = getattr(part, "function_response", None)
                    if response is not None:
                        payload = response.response if isinstance(response.response, dict) else {}
                        yield _sse("tool_result", {
                            "name": response.name,
                            "status": payload.get("status", "ok"),
                            "run_id": payload.get("run_id"),
                            "dashboard_path": payload.get("dashboard_path"),
                            "error": payload.get("error"),
                        })
                        continue
                    text = getattr(part, "text", None)
                    if text:
                        yield _sse("text", {"text": text})
            yield _sse("done", {"session_id": session_id})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


def main() -> None:
    import os

    import uvicorn

    # PORT is how Cloud Run (and most PaaS) tells a container where to listen;
    # HOST must be 0.0.0.0 there and stays on loopback for local demos.
    port = int(os.environ.get("PORT", "8080"))
    host = os.environ.get("HOST", "127.0.0.1")

    status = _auth_status()
    print("\n  Unstructured Data Profiler — demo")
    print(f"  model : {SETTINGS.model}")
    print(f"  auth  : {'OK — ' + status['detail'] if status['ok'] else 'NOT READY — ' + status['detail']}")
    if not status["ok"]:
        print(f"          fix: {status.get('fix', '')}")
        print("          (scanning, the dashboard and the report still work)")
    print(f"\n  http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}\n", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
