"""One command to test everything.

    python run_tests.py              # offline suites only (no auth, no cost)
    python run_tests.py --all        # + the live agent and the deployed service
    python run_tests.py --live       # + live agent against Vertex (spends tokens)
    python run_tests.py --deployed   # + the hosted Cloud Run service

Offline suites need nothing: no credentials, no network, no GCP project. The
live and deployed checks are opt-in because they cost money, and they say
plainly why they were skipped rather than passing silently.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)
if sys.platform == "win32":
    # Enable ANSI on Windows terminals that need it switched on.
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleMode(
            ctypes.windll.kernel32.GetStdHandle(-11), 7
        )
    except Exception:
        GREEN = RED = YELLOW = DIM = BOLD = RESET = ""

OFFLINE_SUITES = [
    ("Profiling passes", "tests/test_profiling.py",
     "sniffing, extraction, PII validators, dedupe, health, scoring"),
    ("Source allowlist", "tests/test_source_allowlist.py",
     "what a public deployment must refuse to scan"),
    ("Scan + tool layer", "tests/test_end_to_end.py",
     "full scan of the synthetic estate, outputs, ADK tools"),
    ("Demo server", "tests/test_demo_server.py",
     "boots the server, drives every endpoint"),
]


def run(label: str, script: str, blurb: str, extra: list[str] | None = None) -> tuple[bool, str]:
    print(f"\n{BOLD}{label}{RESET}  {DIM}{blurb}{RESET}")
    started = time.time()
    result = subprocess.run(
        [sys.executable, script, *(extra or [])],
        cwd=ROOT, capture_output=True, text=True,
    )
    elapsed = time.time() - started
    output = (result.stdout or "") + (result.stderr or "")

    # Surface the suite's own tally when it prints one.
    tally = ""
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if "passed" in stripped and "/" in stripped:
            tally = stripped
            break
        if stripped.startswith(("DEPLOYMENT VERIFIED", "LIVE AGENT OK", "FAILED")):
            tally = stripped
            break

    if result.returncode == 0:
        print(f"  {GREEN}PASS{RESET}  {tally or 'ok'}  {DIM}({elapsed:.1f}s){RESET}")
        return True, tally
    print(f"  {RED}FAIL{RESET}  {tally or 'see output below'}  {DIM}({elapsed:.1f}s){RESET}")
    for line in output.splitlines():
        if line.strip().startswith(("FAIL", "Traceback", "AssertionError", "RuntimeError",
                                    "  File ", "Fix it with", "    gcloud")):
            print(f"        {line.rstrip()}")
    return False, tally


def ensure_sample_estate() -> None:
    if (ROOT / "sample_estate").exists():
        return
    print(f"{DIM}Building the synthetic estate first ...{RESET}")
    subprocess.run(
        [sys.executable, "tools/make_sample_estate.py", "sample_estate"],
        cwd=ROOT, check=True, capture_output=True,
    )


def auth_available() -> tuple[bool, str]:
    import os

    sys.path.insert(0, str(ROOT))
    from estate_profiler.config import SETTINGS  # noqa: E402

    if os.environ.get("GOOGLE_API_KEY"):
        return True, "GOOGLE_API_KEY"
    if not SETTINGS.project_id:
        return False, "GOOGLE_CLOUD_PROJECT is not set"
    try:
        import google.auth
        import google.auth.transport.requests

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        credentials.refresh(google.auth.transport.requests.Request())
        return True, "application default credentials"
    except Exception as exc:
        return False, f"{type(exc).__name__} - run: gcloud auth application-default login"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Unstructured Data Profiler test suites.")
    parser.add_argument("--live", action="store_true", help="also run the live agent check (spends tokens)")
    parser.add_argument("--deployed", action="store_true", help="also verify the Cloud Run service")
    parser.add_argument("--all", action="store_true", help="everything")
    args = parser.parse_args()
    want_live = args.live or args.all
    want_deployed = args.deployed or args.all

    print(f"{BOLD}Unstructured Data Profiler — test run{RESET}")
    print(f"{DIM}python {sys.version.split()[0]} · {ROOT}{RESET}")
    ensure_sample_estate()

    results: list[tuple[str, bool]] = []
    skipped: list[tuple[str, str]] = []

    print(f"\n{DIM}{'─' * 64}{RESET}")
    print(f"{BOLD}OFFLINE{RESET} {DIM}— no credentials, no network, no cost{RESET}")
    for label, script, blurb in OFFLINE_SUITES:
        ok, _ = run(label, script, blurb)
        results.append((label, ok))

    # --- live agent -------------------------------------------------------
    print(f"\n{DIM}{'─' * 64}{RESET}")
    print(f"{BOLD}LIVE{RESET} {DIM}— needs credentials, spends tokens{RESET}")
    if not want_live:
        skipped.append(("Live agent", "not requested (use --live or --all)"))
        print(f"  {YELLOW}SKIP{RESET}  Live agent  {DIM}not requested (--live){RESET}")
    else:
        ok_auth, detail = auth_available()
        if not ok_auth:
            skipped.append(("Live agent", detail))
            print(f"  {YELLOW}SKIP{RESET}  Live agent  {DIM}{detail}{RESET}")
        else:
            print(f"  {DIM}auth: {detail}{RESET}")
            ok, _ = run("Live agent", "tests/smoke_live_agent.py",
                        "real Vertex turns, real tool dispatch")
            results.append(("Live agent", ok))

    # --- deployed service -------------------------------------------------
    print(f"\n{DIM}{'─' * 64}{RESET}")
    print(f"{BOLD}DEPLOYED{RESET} {DIM}— the hosted Cloud Run service{RESET}")
    if not want_deployed:
        skipped.append(("Deployed service", "not requested (use --deployed or --all)"))
        print(f"  {YELLOW}SKIP{RESET}  Deployed service  {DIM}not requested (--deployed){RESET}")
    elif not shutil.which("gcloud") and sys.platform != "win32":
        skipped.append(("Deployed service", "gcloud not on PATH"))
        print(f"  {YELLOW}SKIP{RESET}  Deployed service  {DIM}gcloud not on PATH{RESET}")
    else:
        extra = [] if want_live else ["--skip-agent"]
        ok, _ = run("Deployed service", "deploy/verify_deployment.py",
                    "health, scan, downloads against the hosted instance", extra)
        results.append(("Deployed service", ok))

    # --- summary ----------------------------------------------------------
    print(f"\n{DIM}{'─' * 64}{RESET}")
    passed = sum(1 for _, ok in results if ok)
    failed = [label for label, ok in results if not ok]

    print(f"{BOLD}SUMMARY{RESET}")
    for label, ok in results:
        mark = f"{GREEN}pass{RESET}" if ok else f"{RED}fail{RESET}"
        print(f"  {mark}  {label}")
    for label, why in skipped:
        print(f"  {YELLOW}skip{RESET}  {label} {DIM}— {why}{RESET}")

    print()
    if failed:
        print(f"{RED}{len(failed)} suite(s) failed: {', '.join(failed)}{RESET}")
        return 1
    print(f"{GREEN}All {passed} suite(s) passed.{RESET}")
    if skipped:
        print(f"{DIM}Run `python run_tests.py --all` to include the skipped checks.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
