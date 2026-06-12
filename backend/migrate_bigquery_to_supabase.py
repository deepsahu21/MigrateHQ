"""
One-time migration: copy BigQuery mapping data into Supabase.

Run AFTER setup_supabase.py has created the schema:
  python migrate_bigquery_to_supabase.py
"""
import os
import sys
import logging

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from google.cloud import bigquery
from supabase import create_client

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BQ_PROJECT   = "project-bf89f8dc-434b-4108-be6"
BQ_DATASET   = "migratehq"
RUNS_TABLE   = f"{BQ_PROJECT}.{BQ_DATASET}.mapping_runs"
RESULTS_TABLE = f"{BQ_PROJECT}.{BQ_DATASET}.mapping_results"

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]


def make_display_name(source_dataset: str) -> str:
    name = source_dataset.replace(".csv", "").replace("_", " ")
    return name.title()


def main():
    bq = bigquery.Client(project=BQ_PROJECT)
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    # ── Load from BigQuery ──────────────────────────────────────────────────
    logger.info("Reading BigQuery mapping_runs…")
    bq_runs = list(bq.query(f"SELECT * FROM `{RUNS_TABLE}`").result())
    logger.info("Reading BigQuery mapping_results…")
    bq_results = list(bq.query(f"SELECT * FROM `{RESULTS_TABLE}`").result())
    logger.info("BigQuery: %d runs, %d results", len(bq_runs), len(bq_results))

    # ── Ensure 'olist' tenant exists ────────────────────────────────────────
    tenant_resp = sb.table("tenants").select("id").eq("name", "olist").execute()
    if not tenant_resp.data:
        raise RuntimeError("Tenant 'olist' not found. Did you run setup_supabase.py?")
    olist_tenant_id = tenant_resp.data[0]["id"]
    logger.info("Olist tenant id: %s", olist_tenant_id)

    # ── Create one client per unique source_dataset ─────────────────────────
    unique_sources = {r.source_dataset for r in bq_runs}
    client_id_by_source = {}
    for source in sorted(unique_sources):
        display = make_display_name(source)
        resp = sb.table("clients").upsert(
            {
                "tenant_id":     olist_tenant_id,
                "source_dataset": source,
                "display_name":  display,
            },
            on_conflict="source_dataset",
        ).execute()
        client_id_by_source[source] = resp.data[0]["id"]
        logger.info("Client: %s → %s (%s)", source, display, resp.data[0]["id"][:8])

    # ── Insert mapping_runs ─────────────────────────────────────────────────
    runs_inserted = 0
    for r in bq_runs:
        total = r.total_columns or 1
        l1 = r.l1_count or 0
        l2 = r.l2_count or 0
        accuracy = round((l1 + l2) / total * 100, 2)

        sb.table("mapping_runs").upsert(
            {
                "client_id":     client_id_by_source[r.source_dataset],
                "run_id":        r.run_id,
                "created_at":    str(r.run_timestamp),
                "total_columns": r.total_columns,
                "l1_count":      r.l1_count,
                "l2_count":      r.l2_count,
                "fallback_count": r.fallback_count,
                "accuracy_pct":  accuracy,
                "status":        "completed",
            },
            on_conflict="run_id",
        ).execute()
        runs_inserted += 1

    logger.info("Inserted/upserted %d runs", runs_inserted)

    # ── Insert mapping_results ──────────────────────────────────────────────
    # Batch insert in chunks of 500 to avoid oversized requests
    result_rows = [
        {
            "run_id":           r.run_id,
            "source_column":    r.source_column,
            "target_column":    r.target_column,
            "confidence":       float(r.confidence) if r.confidence is not None else 0.0,
            "layer":            r.layer,
            "correct":          r.correct,
            "flagged_for_review": (r.confidence or 0.0) < 0.75,
        }
        for r in bq_results
    ]

    CHUNK = 500
    for i in range(0, len(result_rows), CHUNK):
        chunk = result_rows[i : i + CHUNK]
        sb.table("mapping_results").insert(chunk).execute()
        logger.info("  inserted results %d–%d", i, i + len(chunk))

    print(
        f"\nMigration complete. "
        f"{len(client_id_by_source)} clients, "
        f"{runs_inserted} runs, "
        f"{len(result_rows)} results migrated."
    )


if __name__ == "__main__":
    main()
