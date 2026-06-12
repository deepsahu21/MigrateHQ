import os
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from google.cloud import bigquery

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

logger = logging.getLogger(__name__)

PROJECT_ID = "project-bf89f8dc-434b-4108-be6"
DATASET_ID = "migratehq"

RUNS_TABLE = f"{PROJECT_ID}.{DATASET_ID}.mapping_runs"
RESULTS_TABLE = f"{PROJECT_ID}.{DATASET_ID}.mapping_results"

RUNS_SCHEMA = [
    bigquery.SchemaField("run_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("run_timestamp", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("source_dataset", "STRING"),
    bigquery.SchemaField("target_dataset", "STRING"),
    bigquery.SchemaField("total_columns", "INTEGER"),
    bigquery.SchemaField("l1_count", "INTEGER"),
    bigquery.SchemaField("l2_count", "INTEGER"),
    bigquery.SchemaField("fallback_count", "INTEGER"),
    bigquery.SchemaField("notes", "STRING"),
]

RESULTS_SCHEMA = [
    bigquery.SchemaField("run_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("source_column", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("target_column", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("confidence", "FLOAT"),
    bigquery.SchemaField("layer", "STRING"),
    bigquery.SchemaField("correct", "BOOLEAN"),
    bigquery.SchemaField("run_timestamp", "TIMESTAMP"),
]


def _ensure_tables(client: bigquery.Client) -> None:
    dataset_ref = bigquery.Dataset(f"{PROJECT_ID}.{DATASET_ID}")
    client.create_dataset(dataset_ref, exists_ok=True)

    for table_id, schema in [(RUNS_TABLE, RUNS_SCHEMA), (RESULTS_TABLE, RESULTS_SCHEMA)]:
        table = bigquery.Table(table_id, schema=schema)
        client.create_table(table, exists_ok=True)


def _run_id_exists(client: bigquery.Client, run_id: str) -> bool:
    query = f"SELECT COUNT(*) AS cnt FROM `{RUNS_TABLE}` WHERE run_id = @run_id"
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
    )
    rows = list(client.query(query, job_config=job_config).result())
    return rows[0].cnt > 0


def push_mapping_to_bigquery(
    mapping_result: dict,
    source_dataset: str,
    target_dataset: str,
    notes: Optional[str] = None,
    run_id: Optional[str] = None,
) -> str:
    """
    Write one orchestrator run to BigQuery.

    Args:
        mapping_result: dict returned by run_orchestrator()
        source_dataset: human label for the source, e.g. "olist_stage1"
        target_dataset: human label for the target, e.g. "manufactured_stage1"
        notes:          optional free-text annotation
        run_id:         if provided and already exists in mapping_runs, skip insert (idempotent)

    Returns:
        run_id on success; raises on failure
    """
    client = bigquery.Client(project=PROJECT_ID)
    _ensure_tables(client)

    if run_id is None:
        run_id = str(uuid.uuid4())
    else:
        if _run_id_exists(client, run_id):
            logger.info("[bigquery_loader] run_id=%s already exists — skipping insert", run_id)
            return run_id

    run_timestamp = datetime.now(timezone.utc).isoformat()

    l1_count = sum(1 for v in mapping_result.values() if v.get("layer") == "L1")
    l2_count = sum(1 for v in mapping_result.values() if v.get("layer") == "L2")
    fallback_count = sum(1 for v in mapping_result.values() if v.get("layer") == "L1-fallback")

    run_row = {
        "run_id": run_id,
        "run_timestamp": run_timestamp,
        "source_dataset": source_dataset,
        "target_dataset": target_dataset,
        "total_columns": len(mapping_result),
        "l1_count": l1_count,
        "l2_count": l2_count,
        "fallback_count": fallback_count,
        "notes": notes,
    }

    result_rows = [
        {
            "run_id": run_id,
            "source_column": src,
            "target_column": info.get("target") or "",
            "confidence": info.get("confidence"),
            "layer": info.get("layer"),
            "correct": None,
            "run_timestamp": run_timestamp,
        }
        for src, info in mapping_result.items()
    ]

    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
    )

    client.load_table_from_json([run_row], RUNS_TABLE, job_config=job_config).result()
    logger.info("[bigquery_loader] run row inserted: run_id=%s", run_id)

    for row in result_rows:
        tgt = row["target_column"] or "NO MATCH"
        logger.info(
            "[bigquery_loader]   %-40s → %-35s [%-12s] conf=%.4f",
            row["source_column"], tgt, row["layer"], row["confidence"] or 0.0,
        )

    client.load_table_from_json(result_rows, RESULTS_TABLE, job_config=job_config).result()
    logger.info("[bigquery_loader] pushed run_id=%s  (%d columns)", run_id, len(result_rows))
    return run_id


def _make_display_name(source_dataset: str) -> str:
    return source_dataset.replace(".csv", "").replace("_", " ").title()


def write_to_supabase(
    mapping_result: dict,
    source_dataset: str,
    run_id: str,
    run_timestamp: Optional[str] = None,
) -> None:
    """
    Mirror one pipeline run to Supabase (clients → mapping_runs → mapping_results).
    Failure logs a warning and returns — never crashes the pipeline.
    """
    try:
        from supabase import create_client

        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SECRET_KEY")
        if not url or not key:
            logger.warning("[supabase] SUPABASE_URL or SUPABASE_SECRET_KEY not set — skipping")
            return

        sb = create_client(url, key)
        ts = run_timestamp or datetime.now(timezone.utc).isoformat()

        # ── Ensure tenant exists ──────────────────────────────────────────────
        tenant_resp = sb.table("tenants").select("id").eq("name", "olist").execute()
        if not tenant_resp.data:
            logger.warning("[supabase] tenant 'olist' not found — skipping")
            return
        tenant_id = tenant_resp.data[0]["id"]

        # ── Upsert client ─────────────────────────────────────────────────────
        client_resp = sb.table("clients").upsert(
            {
                "tenant_id":      tenant_id,
                "source_dataset": source_dataset,
                "display_name":   _make_display_name(source_dataset),
            },
            on_conflict="source_dataset",
        ).execute()
        client_id = client_resp.data[0]["id"]

        # ── Skip if run_id already recorded (idempotency) ─────────────────────
        existing = sb.table("mapping_runs").select("run_id").eq("run_id", run_id).execute()
        if existing.data:
            logger.info("[supabase] run_id=%s already in Supabase — skipping", run_id)
            return

        # ── Insert mapping_run ────────────────────────────────────────────────
        l1 = sum(1 for v in mapping_result.values() if v.get("layer") == "L1")
        l2 = sum(1 for v in mapping_result.values() if v.get("layer") == "L2")
        fb = sum(1 for v in mapping_result.values() if v.get("layer") == "L1-fallback")
        total = len(mapping_result) or 1
        accuracy = round((l1 + l2) / total * 100, 2)

        sb.table("mapping_runs").insert({
            "client_id":      client_id,
            "run_id":         run_id,
            "created_at":     ts,
            "total_columns":  len(mapping_result),
            "l1_count":       l1,
            "l2_count":       l2,
            "fallback_count": fb,
            "accuracy_pct":   accuracy,
            "status":         "completed",
        }).execute()

        # ── Insert mapping_results ────────────────────────────────────────────
        rows = [
            {
                "run_id":           run_id,
                "source_column":    src,
                "target_column":    info.get("target"),
                "confidence":       info.get("confidence"),
                "layer":            info.get("layer"),
                "correct":          None,
                "flagged_for_review": (info.get("confidence") or 0.0) < 0.75,
            }
            for src, info in mapping_result.items()
        ]
        sb.table("mapping_results").insert(rows).execute()
        logger.info("[supabase] wrote run_id=%s (%d columns)", run_id, len(rows))

    except Exception as exc:
        logger.warning("[supabase] write failed (non-fatal): %s", exc)


if __name__ == "__main__":
    mock_mapping = {
        "order_id": {"target": "ord_number", "confidence": 0.89, "layer": "L1"},
        "customer_id": {"target": "cust_ID", "confidence": 0.91, "layer": "L1"},
        "order_status": {"target": "status_code", "confidence": 0.76, "layer": "L2"},
        "order_purchase_timestamp": {"target": "purchase_ts", "confidence": 0.62, "layer": "L1-fallback"},
        "order_delivered_customer_date": {"target": "delivery_date", "confidence": 0.55, "layer": "L2"},
    }

    run_id = push_mapping_to_bigquery(
        mapping_result=mock_mapping,
        source_dataset="olist_orders_raw",
        target_dataset="olist_orders_stage2_man",
        notes="smoke test — mock data",
    )
    print(f"Done. run_id={run_id}")
