"""
End-to-end pipeline test: Stage 1 schema mapping → BigQuery verification.

Stage 1 ground truth (shallow/lexical renames):
  order_id                      → ord_number
  customer_id                   → cust_ID
  order_status                  → order_state
  order_purchase_timestamp      → purchase_ts
  order_approved_at             → approved_at
  order_delivered_carrier_date  → carrier_delivery_date
  order_delivered_customer_date → customer_delivery_date
  order_estimated_delivery_date → estimated_delivery_date
"""
import os
import sys
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from google.cloud import bigquery

from orchestrator_pipeline import run_mapping_pipeline

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

PROJECT_ID = "project-bf89f8dc-434b-4108-be6"
DATASET_ID = "migratehq"
RESULTS_TABLE = f"{PROJECT_ID}.{DATASET_ID}.mapping_results"

SOURCE_CSV = "data/raw/olist_orders_dataset.csv"
TARGET_CSV = "data/manufactured/olist_orders_stage1_man.csv"

GROUND_TRUTH = {
    "order_id": "ord_number",
    "customer_id": "cust_ID",
    "order_status": "order_state",
    "order_purchase_timestamp": "purchase_ts",
    "order_approved_at": "approved_at",
    "order_delivered_carrier_date": "carrier_delivery_date",
    "order_delivered_customer_date": "customer_delivery_date",
    "order_estimated_delivery_date": "estimated_delivery_date",
}


def _query_results(run_id: str) -> list[dict]:
    client = bigquery.Client(project=PROJECT_ID)
    query = f"""
        SELECT source_column, target_column, confidence, layer
        FROM `{RESULTS_TABLE}`
        WHERE run_id = @run_id
        ORDER BY source_column
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
    )
    return [dict(row) for row in client.query(query, job_config=job_config).result()]


def _print_results(rows: list[dict]) -> tuple[int, int]:
    W = [35, 35, 8, 14, 9]
    headers = ["source_column", "target_column", "conf", "layer", "correct?"]
    sep = "+-" + "-+-".join("-" * w for w in W) + "-+"
    hdr = "| " + " | ".join(h.ljust(w) for h, w in zip(headers, W)) + " |"
    print(sep)
    print(hdr)
    print(sep)

    correct = 0
    for row in rows:
        src = row["source_column"]
        tgt = row["target_column"] if row["target_column"] else "NO MATCH"
        conf = f"{row['confidence']:.4f}" if row["confidence"] is not None else "—"
        layer = row["layer"] or "—"
        expected = GROUND_TRUTH.get(src)
        hit = expected is not None and row["target_column"] == expected
        if hit:
            correct += 1
        cells = [
            src.ljust(W[0]),
            tgt.ljust(W[1]),
            conf.ljust(W[2]),
            layer.ljust(W[3]),
            ("YES" if hit else ("—" if expected is None else "NO")).ljust(W[4]),
        ]
        print("| " + " | ".join(cells) + " |")

    print(sep)
    return correct, len([r for r in rows if r["source_column"] in GROUND_TRUTH])


if __name__ == "__main__":
    print("\n=== MigrateHQ E2E Pipeline Test — Stage 1 ===\n")
    print(f"Source: {SOURCE_CSV}")
    print(f"Target: {TARGET_CSV}\n")

    result = run_mapping_pipeline(
        source_csv_path=SOURCE_CSV,
        target_csv_path=TARGET_CSV,
        dataset_name="olist_stage1",
        sample_size=100,
        notes="e2e test — stage 1 shallow renames",
        # Stage 1 target CSV has a spurious title row before the real header
        target_csv_kwargs={"skiprows": 1},
    )

    if result["status"] == "error":
        print(f"\nPipeline FAILED: {result['error']}")
        sys.exit(1)

    run_id = result["run_id"]
    print(f"\nPipeline succeeded.  run_id={run_id}")
    print(f"  total_columns : {result['total_columns']}")
    print(f"  L1 matches    : {result['l1_count']}")
    print(f"  L2 matches    : {result['l2_count']}")
    print(f"  L1-fallback   : {result['fallback_count']}")

    print("\nQuerying BigQuery for results...\n")
    rows = _query_results(run_id)

    if not rows:
        print("ERROR: No rows found in BigQuery for this run_id.")
        sys.exit(1)

    correct, total_gt = _print_results(rows)

    print(f"\nBigQuery row count  : {len(rows)}")
    print(f"Ground-truth columns: {total_gt}")
    print(f"Correct mappings    : {correct}/{total_gt}")

    l1_bq = sum(1 for r in rows if r["layer"] == "L1")
    l2_bq = sum(1 for r in rows if r["layer"] == "L2")
    fb_bq = sum(1 for r in rows if r["layer"] == "L1-fallback")
    print(f"Layer breakdown     : L1={l1_bq}  L2={l2_bq}  fallback={fb_bq}")

    print(
        f"\nPipeline connected. Stage 1 test: {len(rows)} rows inserted, "
        f"{l1_bq} L1 matches, {l2_bq} L2 matches"
    )

    if correct == total_gt:
        print(f"*** {correct}/{total_gt} — Stage 1 PASSED ***")
    else:
        missed = [
            r["source_column"]
            for r in rows
            if r["source_column"] in GROUND_TRUTH
            and (r["target_column"] or "") != GROUND_TRUTH[r["source_column"]]
        ]
        print(f"Stage 1 PARTIAL — misses: {missed}")
