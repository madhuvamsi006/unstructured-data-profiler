"""End-to-end tests: scan the generated sample estate and exercise the ADK tools.

Run with: python tests/test_end_to_end.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from estate_profiler import tools  # noqa: E402
from estate_profiler.config import SETTINGS  # noqa: E402
from estate_profiler.engine import RunStore, scan_estate  # noqa: E402
from estate_profiler.outputs import bq, dashboard, report  # noqa: E402
from make_sample_estate import build  # noqa: E402


class FakeToolContext:
    """Stand-in for ADK's ToolContext: the tools only use .state."""

    def __init__(self):
        self.state: dict = {}


_tmp = Path(tempfile.mkdtemp(prefix="estate-e2e-"))
ESTATE = _tmp / "estate"
SETTINGS.run_root = _tmp / "runs"
build(ESTATE)

SUMMARY = scan_estate(str(ESTATE), settings=SETTINGS)
RUN_ID = SUMMARY["run_id"]


# --------------------------------------------------------------------------
# engine
# --------------------------------------------------------------------------

def test_scan_finds_every_file_in_the_sample_estate():
    on_disk = sum(1 for p in ESTATE.rglob("*") if p.is_file())
    assert SUMMARY["inventory"]["total_files"] == on_disk


def test_manifest_has_one_row_per_file_with_a_disposition():
    rows = RunStore(RUN_ID, SETTINGS).read_manifest()
    assert len(rows) == SUMMARY["inventory"]["total_files"]
    valid = {"ingest_now", "ingest_after_processing", "remediate", "quarantine", "exclude"}
    assert all(r["disposition"] in valid for r in rows)
    assert all(0 <= r["readiness_score"] <= 100 for r in rows)


def test_dispositions_sum_to_the_file_count():
    total = sum(d["count"] for d in SUMMARY["readiness"]["by_disposition"])
    assert total == SUMMARY["inventory"]["total_files"]


def test_sensitivity_levels_sum_to_the_file_count():
    total = sum(d["count"] for d in SUMMARY["sensitivity"]["by_level"])
    assert total == SUMMARY["inventory"]["total_files"]


def test_planted_credentials_are_found_and_quarantined():
    rows = RunStore(RUN_ID, SETTINGS).read_manifest()
    cred_rows = [r for r in rows if "credential" in r["sensitivity_categories"]]
    names = {r["name"] for r in cred_rows}
    assert "deploy.env" in names
    assert "service-account.json" in names
    assert all(r["disposition"] == "quarantine" for r in cred_rows)


def test_planted_format_problems_are_all_detected():
    rows = {r["name"]: r for r in RunStore(RUN_ID, SETTINGS).read_manifest()}
    assert "zero_byte" in rows["empty_placeholder.docx"]["health_issues"]
    assert "encrypted" in rows["locked_board_pack.pdf"]["health_issues"]
    assert "extension_mismatch" in rows["quarterly_report.pdf"]["health_issues"]
    assert "legacy_format" in rows["budget_fy19.xls"]["health_issues"]
    assert "archive_unexpanded" in rows["archive_2016.zip"]["health_issues"]
    assert "not_a_document" in rows["setup_installer.exe"]["health_issues"]
    assert "no_extension" in rows["notes"]["health_issues"]


def test_scanned_documents_are_routed_to_document_ai():
    rows = {r["name"]: r for r in RunStore(RUN_ID, SETTINGS).read_manifest()}
    scanned = rows["signed_offer_letter.pdf"]
    assert scanned["needs_ocr"]
    assert scanned["disposition"] == "ingest_after_processing"
    assert scanned["recommended_processor"] in ("document-ocr", "form-parser")


def test_planted_duplicates_are_found():
    assert SUMMARY["duplication"]["exact_groups"] >= 3
    assert SUMMARY["duplication"]["near_groups"] >= 2
    assert SUMMARY["duplication"]["redundant_bytes"] > 0


def test_pii_heavy_file_outranks_clean_file():
    rows = {r["name"]: r for r in RunStore(RUN_ID, SETTINGS).read_manifest()}
    assert rows["employee_roster_2024.docx"]["sensitivity_score"] > 0
    assert rows["failover_runbook.md"]["sensitivity_score"] == 0


def test_max_files_truncates_and_is_reported():
    capped = scan_estate(str(ESTATE), settings=SETTINGS, max_files=5)
    assert capped["inventory"]["total_files"] == 5
    assert capped["scan"]["truncated"] is True


# --------------------------------------------------------------------------
# outputs
# --------------------------------------------------------------------------

def test_markdown_report_contains_every_section():
    markdown = report.build_markdown(SUMMARY)
    for heading in ("Executive summary", "Disposition", "Inventory", "Duplication",
                    "Sensitivity", "Format health", "readiness", "Recommended next steps"):
        assert heading in markdown, f"missing section: {heading}"
    assert "|" in markdown  # tables rendered


def test_dashboard_is_self_contained_and_embeds_the_data():
    html = dashboard.build_html(SUMMARY)
    assert "__DATA__" not in html and "__TITLE__" not in html
    assert SUMMARY["run_id"] in html
    assert "<title>" in html
    # No external script/style beyond the permitted Google Fonts stylesheet.
    assert "cdnjs" not in html and "unpkg" not in html
    assert html.count("<script") >= 2


def test_dashboard_escapes_script_terminator_in_embedded_json():
    hostile = dict(SUMMARY)
    hostile["source_uri"] = "</script><img src=x onerror=alert(1)>"
    html = dashboard.build_html(hostile)
    assert "</script><img" not in html


def test_bq_rows_match_the_declared_schema():
    rows = RunStore(RUN_ID, SETTINGS).read_manifest()
    allowed = {name for name, _, _ in bq.SCHEMA_FIELDS}
    required = {name for name, _, mode in bq.SCHEMA_FIELDS if mode == "REQUIRED"}
    for record in rows:
        row = bq.to_bq_row(record)
        assert set(row) <= allowed, set(row) - allowed
        assert required <= set(row)
        assert isinstance(row["health_issues"], list)
        assert isinstance(row["sensitivity_findings_json"], str)


# --------------------------------------------------------------------------
# ADK tool layer
# --------------------------------------------------------------------------

def test_scan_tool_returns_headline_and_records_run_in_state():
    ctx = FakeToolContext()
    result = tools.scan_unstructured_estate(str(ESTATE), 0, 0, False, ctx)
    assert result["status"] == "ok"
    assert ctx.state["estate_run_id"] == result["run_id"]
    assert result["headline"]["total_files"] > 0
    # The manifest must never be returned to the model.
    assert "files" not in result and "manifest" not in result


def test_scan_tool_reports_a_bad_source_as_an_error():
    ctx = FakeToolContext()
    result = tools.scan_unstructured_estate("./no-such-estate-xyz", 0, 0, False, ctx)
    assert result["status"] == "error"
    assert "error" in result


def test_findings_tool_rejects_an_unknown_dimension():
    ctx = FakeToolContext()
    ctx.state["estate_run_id"] = RUN_ID
    assert tools.get_estate_findings("nonsense", "", ctx)["status"] == "error"
    assert tools.get_estate_findings("sensitivity", "", ctx)["status"] == "ok"


def test_query_tool_filters_and_caps_results():
    ctx = FakeToolContext()
    ctx.state["estate_run_id"] = RUN_ID
    result = tools.query_estate_files(RUN_ID, "quarantine", "", "", "", 0, ctx)
    assert result["status"] == "ok"
    assert result["returned"] <= result["match_count"]
    assert all(f["disposition"] == "quarantine" for f in result["files"])

    capped = tools.query_estate_files(RUN_ID, "", "", "", "", 500, ctx)
    assert capped["returned"] <= 100

    scoped = tools.query_estate_files(RUN_ID, "", "", "", "finance", 0, ctx)
    assert all("finance" in f["rel_path"].lower() for f in scoped["files"])


def test_query_tool_never_returns_matched_sensitive_values():
    ctx = FakeToolContext()
    result = tools.query_estate_files(RUN_ID, "quarantine", "", "", "", 0, ctx)
    blob = str(result)
    # The planted secrets must not leak back through the tool surface.
    assert "AKIAIOSFODNN7EXAMPLE" not in blob
    assert "123-45-6789" not in blob
    assert "4111" not in blob


def test_report_and_dashboard_tools_write_files():
    ctx = FakeToolContext()
    ctx.state["estate_run_id"] = RUN_ID
    out = _tmp / "deliverables"

    reported = tools.generate_estate_report(RUN_ID, str(out), ctx)
    assert reported["status"] == "ok"
    assert Path(reported["markdown_path"]).exists()
    assert Path(reported["json_path"]).exists()
    assert reported["recommended_steps"]

    built = tools.build_estate_dashboard(RUN_ID, str(out / "dashboard.html"), ctx)
    assert built["status"] == "ok"
    assert Path(built["dashboard_path"]).exists()
    assert built["size_kb"] > 0


def test_tools_fail_cleanly_when_no_scan_exists():
    empty = Path(tempfile.mkdtemp(prefix="estate-empty-"))
    original = SETTINGS.run_root
    SETTINGS.run_root = empty / "runs"
    try:
        ctx = FakeToolContext()
        assert tools.get_estate_findings("inventory", "", ctx)["status"] == "error"
        assert tools.query_estate_files("", "", "", "", "", 0, ctx)["status"] == "error"
        assert tools.generate_estate_report("", "", ctx)["status"] == "error"
        assert tools.list_estate_runs(ctx)["runs"] == []
    finally:
        SETTINGS.run_root = original
        shutil.rmtree(empty, ignore_errors=True)


def test_list_runs_returns_the_completed_scan():
    ctx = FakeToolContext()
    result = tools.list_estate_runs(ctx)
    assert result["status"] == "ok"
    assert any(r["run_id"] == RUN_ID for r in result["runs"])


def test_bigquery_tool_refuses_without_a_project():
    ctx = FakeToolContext()
    ctx.state["estate_run_id"] = RUN_ID
    original = SETTINGS.project_id
    SETTINGS.project_id = ""
    try:
        result = tools.load_inventory_to_bigquery(RUN_ID, "", "", "", ctx)
        assert result["status"] == "error"
        assert "GOOGLE_CLOUD_PROJECT" in result["error"]
    finally:
        SETTINGS.project_id = original


def test_bigquery_tool_validates_write_disposition():
    ctx = FakeToolContext()
    original = SETTINGS.project_id
    SETTINGS.project_id = "fake-project"
    try:
        result = tools.load_inventory_to_bigquery(RUN_ID, "", "", "WRITE_SOMETHING", ctx)
        assert result["status"] == "error"
        assert "WRITE_APPEND" in result["error"]
    finally:
        SETTINGS.project_id = original


# --------------------------------------------------------------------------
# agent wiring
# --------------------------------------------------------------------------

def test_root_agent_exposes_every_tool():
    from estate_profiler.agent import root_agent

    assert root_agent.name == "unstructured_data_profiler"
    names = set()
    for tool in root_agent.tools:
        names.add(getattr(tool, "__name__", None) or getattr(tool, "name", ""))
    for expected in (
        "scan_unstructured_estate", "get_estate_findings", "query_estate_files",
        "generate_estate_report", "build_estate_dashboard",
        "load_inventory_to_bigquery", "list_estate_runs",
    ):
        assert expected in names, f"tool not wired: {expected}"


if __name__ == "__main__":
    import traceback

    tests = [(n, o) for n, o in sorted(globals().items()) if n.startswith("test_") and callable(o)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception:
            failures += 1
            print(f"  FAIL  {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    shutil.rmtree(_tmp, ignore_errors=True)
    sys.exit(1 if failures else 0)
