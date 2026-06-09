import uuid
from datetime import datetime, timezone

from google.cloud import bigquery

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


def push_mapping_to_bigquery(
    mapping_result: dict,
    source_dataset: str,
    target_dataset: str,
    notes: str = None,
) -> str:
    """
    Write one orchestrator run to BigQuery.

    Args:
        mapping_result: dict returned by run_orchestrator()
        source_dataset: human label for the source, e.g. "olist_stage1"
        target_dataset: human label for the target, e.g. "manufactured_stage1"
        notes: optional free-text annotation

    Returns:
        run_id on success; raises on failure
    """
    client = bigquery.Client(project=PROJECT_ID)
    _ensure_tables(client)

    run_id = str(uuid.uuid4())
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
            "target_column": info.get("target"),
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
    client.load_table_from_json(result_rows, RESULTS_TABLE, job_config=job_config).result()

    print(f"[bigquery_loader] pushed run_id={run_id}  ({len(result_rows)} columns)")
    return run_id


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
        target_dataset="olist_orders_stage2_manufactured",
        notes="smoke test — mock data",
    )
    print(f"Done. run_id={run_id}")
