"""Verify a deployed Cloud Run instance actually works.

Deploying successfully and *working* are different claims. This drives the
hosted service the way the demo will: health, a real scan against the bundled
estate, every download, and — if credentials allow — a live agent turn.

    python deploy/verify_deployment.py                 # auto-discovers the URL
    python deploy/verify_deployment.py --url https://...
    python deploy/verify_deployment.py --url http://127.0.0.1:8080  # via proxy
    python deploy/verify_deployment.py --skip-agent    # no tokens spent
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_SERVICE = "estate-profiler-demo"
DEFAULT_REGION = "asia-southeast1"


def gcloud(*args: str) -> str:
    result = subprocess.run(
        ["gcloud", *args], capture_output=True, text=True, shell=(sys.platform == "win32")
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "gcloud failed")
    return result.stdout.strip()


def discover(service: str, region: str, project: str | None) -> tuple[str, str | None]:
    """Find the service URL and an identity token for the private service."""
    args = ["run", "services", "describe", service, "--region", region, "--format", "value(status.url)"]
    if project:
        args += ["--project", project]
    url = gcloud(*args)
    try:
        token = gcloud("auth", "print-identity-token")
    except RuntimeError:
        token = None
    return url, token


class Client:
    def __init__(self, base: str, token: str | None):
        self.base = base.rstrip("/")
        self.token = token

    def _request(self, path: str, data: dict | None = None) -> urllib.request.Request:
        request = urllib.request.Request(self.base + path)
        # A proxy tunnel injects credentials itself; a direct call needs the token.
        if self.token and not self.base.startswith("http://127.0.0.1"):
            request.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            request.data = json.dumps(data).encode()
            request.add_header("Content-Type", "application/json")
        return request

    def get_json(self, path: str, timeout: int = 120) -> dict:
        with urllib.request.urlopen(self._request(path), timeout=timeout) as response:
            return json.loads(response.read())

    def raw(self, path: str, timeout: int = 120) -> tuple[int, bytes, str]:
        with urllib.request.urlopen(self._request(path), timeout=timeout) as response:
            return response.status, response.read(), response.headers.get("content-type", "")

    def sse(self, path: str, data: dict | None = None, timeout: int = 900) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        with urllib.request.urlopen(self._request(path, data), timeout=timeout) as response:
            current = None
            for raw in response:
                line = raw.decode("utf-8").rstrip()
                if line.startswith("event: "):
                    current = line[7:]
                elif line.startswith("data: ") and current:
                    events.append((current, json.loads(line[6:])))
        return events


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="")
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--project", default="")
    parser.add_argument("--skip-agent", action="store_true", help="skip the live model turn")
    args = parser.parse_args()

    token = None
    url = args.url
    if not url:
        print("Discovering the service URL ...")
        try:
            url, token = discover(args.service, args.region, args.project or None)
        except RuntimeError as exc:
            print(f"Could not find the service: {exc}")
            print("Pass --url explicitly, or deploy first with deploy\\deploy.ps1")
            return 2
    elif not url.startswith("http://127.0.0.1"):
        try:
            token = gcloud("auth", "print-identity-token")
        except RuntimeError:
            token = None

    print(f"Target: {url}\n")
    client = Client(url, token)
    failures: list[str] = []

    def check(label: str, fn) -> object:
        nonlocal failures
        try:
            started = time.time()
            result = fn()
            print(f"  PASS  {label}  ({time.time() - started:.1f}s)")
            return result
        except Exception as exc:
            failures.append(label)
            detail = exc
            if isinstance(exc, urllib.error.HTTPError):
                body = exc.read()[:300].decode(errors="replace")
                detail = f"HTTP {exc.code} {body}"
            print(f"  FAIL  {label}: {detail}")
            return None

    # --- health -----------------------------------------------------------
    health = check("health responds", lambda: client.get_json("/api/health"))
    if health is None:
        print("\nCannot reach the service.")
        if token is None:
            print("The service is private and no identity token was available.")
            print("Run `gcloud auth login`, or tunnel with deploy\\proxy.ps1 and")
            print("re-run against --url http://127.0.0.1:8080")
        return 1

    print(f"        environment : {health.get('environment')}")
    print(f"        revision    : {health.get('revision')}")
    print(f"        model       : {health.get('model')}")
    print(f"        auth        : {health['auth']['ok']} - {health['auth']['detail']}")

    check("runtime is Cloud Run", lambda: _assert(
        health.get("environment") == "cloud_run",
        f"environment is {health.get('environment')}",
    ))
    check("runtime credentials work", lambda: _assert(
        health["auth"]["ok"], health["auth"]["detail"]
    ))
    check("sample estate is bundled", lambda: _assert(
        bool(health.get("sample_estate")), "no sample estate in the image"
    ))
    check("UI is served", lambda: _assert(
        client.raw("/")[0] == 200 and b"Unstructured Data Profiler" in client.raw("/")[1],
        "index did not render",
    ))

    # --- a real scan ------------------------------------------------------
    sample = health.get("sample_estate") or "sample_estate"

    def run_scan():
        query = urllib.parse.urlencode({"source": sample, "max_files": 0})
        events = client.sse("/api/scan?" + query)
        kinds = [kind for kind, _ in events]
        if "error" in kinds:
            raise RuntimeError(next(p["message"] for k, p in events if k == "error"))
        if "done" not in kinds:
            raise RuntimeError(f"no done event: {kinds}")
        return events[-1][1]

    summary = check("scan streams and completes", run_scan)

    if summary:
        inv = summary["inventory"]
        print(f"        files       : {inv['total_files']}")
        print(f"        ingest-ready: {summary['readiness']['ingest_now_pct']}%")
        print(f"        credentials : {summary['sensitivity']['credential_exposures']}")

        run_id = summary["run_id"]
        for path, expect in [
            ("/summary", "application/json"),
            ("/dashboard", "text/html"),
            ("/report", "text/markdown"),
            ("/manifest", "application/x-ndjson"),
        ]:
            check(f"download {path}", lambda p=path, e=expect: _assert(
                (lambda r: r[0] == 200 and e in r[2] and len(r[1]) > 200)(
                    client.raw(f"/api/runs/{run_id}{p}")
                ),
                "unexpected status, type or size",
            ))

        check("dashboard leaks no secrets", lambda: _assert(
            not [
                s for s in ("AKIAIOSFODNN7EXAMPLE", "123-45-6789", "4111 1111", "hunter2demo")
                if s in client.raw(f"/api/runs/{run_id}/dashboard")[1].decode("utf-8", "replace")
            ],
            "a planted secret appeared in the dashboard",
        ))
        check("DLP stayed opt-in", lambda: _assert(
            summary["scan"]["dlp_used"] is False, "DLP ran without being asked"
        ))

    # --- the allowlist, which is what makes a public URL acceptable --------
    allowed = health.get("allowed_sources") or []
    if not allowed:
        print("  WARN  no source allowlist configured - any source is scannable")
    else:
        print(f"        allowlist   : {', '.join(allowed)}")

        def refuses_arbitrary_source():
            """A visitor must not be able to point this at any bucket."""
            query = urllib.parse.urlencode(
                {"source": "gs://some-bucket-that-should-be-refused", "max_files": 10}
            )
            events = client.sse("/api/scan?" + query, timeout=120)
            kinds = [kind for kind, _ in events]
            if "done" in kinds:
                raise AssertionError("an arbitrary bucket was accepted for scanning")
            if "error" not in kinds:
                raise AssertionError(f"expected a refusal, got {kinds}")
            message = next(p["message"] for k, p in events if k == "error")
            if "not permitted" not in message:
                raise AssertionError(f"refused, but not by the allowlist: {message}")
            return True

        check("refuses sources outside the allowlist", refuses_arbitrary_source)

        def refuses_path_escape():
            """.. must not climb out of an allowed directory."""
            query = urllib.parse.urlencode(
                {"source": (allowed[0].rstrip("/") + "/../.."), "max_files": 10}
            )
            events = client.sse("/api/scan?" + query, timeout=120)
            kinds = [kind for kind, _ in events]
            if "done" in kinds:
                raise AssertionError("a path escape was accepted for scanning")
            return True

        if not allowed[0].startswith("gs://"):
            check("refuses path escapes", refuses_path_escape)

    # --- the agent --------------------------------------------------------
    if args.skip_agent:
        print("  SKIP  live agent turn (--skip-agent)")
    elif not health["auth"]["ok"]:
        print("  SKIP  live agent turn (runtime credentials are not working)")
        failures.append("live agent turn (unreachable)")
    else:
        def agent_turn():
            events = client.sse("/api/chat", data={"message": "What do you check for? Answer in two sentences."})
            errors = [p for k, p in events if k == "error"]
            if errors:
                raise RuntimeError(errors[0].get("message", "unknown"))
            text = "".join(p["text"] for k, p in events if k == "text").strip()
            if not text:
                raise RuntimeError("agent returned no text")
            return text

        answer = check("live agent answers", agent_turn)
        if answer:
            print(f'        "{answer[:150]}..."')

    # --- verdict ----------------------------------------------------------
    print()
    if failures:
        print(f"FAILED ({len(failures)}): {', '.join(failures)}")
        return 1
    print("DEPLOYMENT VERIFIED — the hosted demo works end to end.")
    return 0


def _assert(condition: bool, message: str) -> bool:
    if not condition:
        raise AssertionError(message)
    return True


if __name__ == "__main__":
    raise SystemExit(main())
