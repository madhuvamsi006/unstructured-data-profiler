"""Load the per-file manifest into BigQuery so large estates stay queryable.

The manifest is the asset that outlives the run: once it is a table, a client
can ask "show me every high-sensitivity file under /Finance older than 5 years"
without re-scanning anything.
"""

from __future__ import annotations

import json
from pathlib import Path

# Nested detector counts vary per file, so findings are stored as a JSON string
# rather than a fixed schema. Everything used for filtering stays a real column.
SCHEMA_FIELDS = [
    ("run_id", "STRING", "REQUIRED"),
    ("uri", "STRING", "REQUIRED"),
    ("source", "STRING", "NULLABLE"),
    ("container", "STRING", "NULLABLE"),
    ("rel_path", "STRING", "NULLABLE"),
    ("parent", "STRING", "NULLABLE"),
    ("name", "STRING", "NULLABLE"),
    ("ext", "STRING", "NULLABLE"),
    ("size_bytes", "INT64", "NULLABLE"),
    ("modified", "TIMESTAMP", "NULLABLE"),
    ("age_days", "INT64", "NULLABLE"),
    ("depth", "INT64", "NULLABLE"),
    ("sniffed_type", "STRING", "NULLABLE"),
    ("extension_mismatch", "BOOL", "NULLABLE"),
    ("content_hash", "STRING", "NULLABLE"),
    ("hash_scope", "STRING", "NULLABLE"),
    ("text_sketch", "STRING", "NULLABLE"),
    ("dup_status", "STRING", "NULLABLE"),
    ("text_chars", "INT64", "NULLABLE"),
    ("extract_method", "STRING", "NULLABLE"),
    ("extractable", "BOOL", "NULLABLE"),
    ("needs_ocr", "BOOL", "NULLABLE"),
    ("page_hint", "INT64", "NULLABLE"),
    ("health_status", "STRING", "NULLABLE"),
    ("health_score", "INT64", "NULLABLE"),
    ("health_issues", "STRING", "REPEATED"),
    ("remediation", "STRING", "NULLABLE"),
    ("sensitivity_score", "INT64", "NULLABLE"),
    ("sensitivity_level", "STRING", "NULLABLE"),
    ("sensitivity_categories", "STRING", "REPEATED"),
    ("sensitivity_findings_json", "STRING", "NULLABLE"),
    ("content_classes", "STRING", "REPEATED"),
    ("dlp_checked", "BOOL", "NULLABLE"),
    ("dlp_infotypes_json", "STRING", "NULLABLE"),
    ("readiness_score", "INT64", "NULLABLE"),
    ("readiness_band", "STRING", "NULLABLE"),
    ("disposition", "STRING", "NULLABLE"),
    ("recommended_processor", "STRING", "NULLABLE"),
    ("est_tokens", "INT64", "NULLABLE"),
    ("est_chunks", "INT64", "NULLABLE"),
    ("blocking_reason", "STRING", "NULLABLE"),
]

_SCHEMA_KEYS = {name for name, _, _ in SCHEMA_FIELDS}


def to_bq_row(record: dict) -> dict:
    """Flatten a manifest record into the BigQuery schema."""
    row = {k: v for k, v in record.items() if k in _SCHEMA_KEYS}
    row["sensitivity_findings_json"] = json.dumps(record.get("sensitivity_findings") or {})
    row["dlp_infotypes_json"] = json.dumps(record.get("dlp_infotypes") or {})
    row.setdefault("health_issues", [])
    row.setdefault("sensitivity_categories", [])
    row.setdefault("content_classes", [])
    return row


def load_manifest(
    manifest_path: Path,
    *,
    project_id: str,
    dataset: str,
    table: str,
    location: str = "US",
    write_disposition: str = "WRITE_APPEND",
) -> dict:
    """Create the dataset/table if needed and load every manifest row.

    Partitioned by scan date and clustered on the columns clients actually
    filter by, so a 10M-row inventory stays cheap to query.
    """
    from google.cloud import bigquery

    client = bigquery.Client(project=project_id)
    dataset_ref = bigquery.DatasetReference(project_id, dataset)
    try:
        client.get_dataset(dataset_ref)
    except Exception:
        new_dataset = bigquery.Dataset(dataset_ref)
        new_dataset.location = location
        client.create_dataset(new_dataset, exists_ok=True)

    schema = [bigquery.SchemaField(n, t, mode=m) for n, t, m in SCHEMA_FIELDS]
    table_id = f"{project_id}.{dataset}.{table}"

    rows = []
    with open(manifest_path, encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(to_bq_row(json.loads(line)))
    if not rows:
        return {"status": "skipped", "reason": "manifest is empty", "table": table_id, "rows": 0}

    job_config = bigquery.LoadJobConfig(
        schema=schema,
        write_disposition=write_disposition,
        time_partitioning=bigquery.TimePartitioning(field="modified"),
        clustering_fields=["run_id", "disposition", "sensitivity_level", "ext"],
    )
    job = client.load_table_from_json(rows, table_id, job_config=job_config)
    job.result()

    return {
        "status": "loaded",
        "table": table_id,
        "rows": len(rows),
        "console_url": f"https://console.cloud.google.com/bigquery?project={project_id}"
                       f"&ws=!1m5!1m4!4m3!1s{project_id}!2s{dataset}!3s{table}",
        "sample_query": (
            f"SELECT disposition, sensitivity_level, COUNT(*) AS files, "
            f"ROUND(SUM(size_bytes)/POW(1024,3), 2) AS gb\n"
            f"FROM `{table_id}`\nWHERE run_id = '{rows[0]['run_id']}'\n"
            f"GROUP BY 1, 2 ORDER BY files DESC"
        ),
    }
