"""
Stage 3 test — zero lexical overlap; DispatchEvent/CompletionEvent deliberately ambiguous.

Ground truth:
  order_id                      → RecordKey
  customer_id                   → PartyRef
  order_status                  → LifecyclePhase
  order_purchase_timestamp      → InitiationTime
  order_approved_at             → ConfirmedWhen
  order_delivered_carrier_date  → DispatchEvent
  order_delivered_customer_date → CompletionEvent
  order_estimated_delivery_date → WindowClose

Strategy:
  - L1 (Valentine Jaccard) scores 0/8 — no lexical overlap anywhere.
  - L2 handles all columns; DispatchEvent vs CompletionEvent are deliberately ambiguous
    (both look like delivery events) so expect potential fallback or low confidence there.
  - Full pipeline writes results to BigQuery + Supabase (client label: olist_orders_stage3_man).
"""
import sys
import os
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from google.cloud import bigquery

from orchestrator_pipeline import run_mapping_pipeline
from orchestrator import print_results_table

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

PROJECT_ID = "project-bf89f8dc-434b-4108-be6"
DATASET_ID = "migratehq"
RESULTS_TABLE = f"{PROJECT_ID}.{DATASET_ID}.mapping_results"

SOURCE_CSV = "data/raw/olist_orders_dataset.csv"
TARGET_CSV = "data/manufactured/olist_orders_stage3_man.csv"
CLIENT_LABEL = "olist_orders_stage3_man"

GROUND_TRUTH = {
    "order_id":                      "RecordKey",
    "customer_id":                   "PartyRef",
    "order_status":                  "LifecyclePhase",
    "order_purchase_timestamp":      "InitiationTime",
    "order_approved_at":             "ConfirmedWhen",
    "order_delivered_carrier_date":  "DispatchEvent",
    "order_delivered_customer_date": "CompletionEvent",
    "order_estimated_delivery_date": "WindowClose",
}


def _score_by_layer(mapping: dict, ground_truth: dict) -> dict:
    """Return {layer_prefix: (correct, total)} for each distinct layer group."""
    buckets: dict[str, list[bool]] = {}
    for src, info in mapping.items():
        if src not in ground_truth:
            continue
        layer = info.get("layer", "none")
        hit = info.get("target") == ground_truth[src]
        buckets.setdefault(layer, []).append(hit)
    return {layer: (sum(hits), len(hits)) for layer, hits in buckets.items()}


def _query_bq(run_id: str) -> list[dict]:
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


if __name__ == "__main__":
    print("\n=== MigrateHQ Stage 3 Test: Chaos Renames ===\n")
    print(f"Source : {SOURCE_CSV}")
    print(f"Target : {TARGET_CSV}")
    print(f"Client : {CLIENT_LABEL}\n")

    # ── Run pipeline (L1 → L2 → BigQuery → Supabase) ─────────────────────────
    result = run_mapping_pipeline(
        source_csv_path=SOURCE_CSV,
        target_csv_path=TARGET_CSV,
        dataset_name=CLIENT_LABEL,
        sample_size=100,
        notes="stage3 chaos renames",
        client_label=CLIENT_LABEL,
    )

    if result["status"] == "error":
        print(f"\nPipeline FAILED: {result['error']}")
        sys.exit(1)

    run_id = result["run_id"]
    mapping = result["mapping"]
    print(f"\nPipeline succeeded.  run_id={run_id}")
    print(f"  total_columns : {result['total_columns']}")
    print(f"  L1 matches    : {result['l1_count']}")
    print(f"  L2 matches    : {result['l2_count']}")
    print(f"  L1-fallback   : {result['fallback_count']}")

    # ── Per-column results with ground-truth check ───────────────────────────
    print()
    print_results_table(mapping, ground_truth=GROUND_TRUTH)

    # ── Per-column layer attribution ─────────────────────────────────────────
    print("\n=== Per-Column Layer Attribution ===\n")
    W = [35, 22, 14, 8, 8]
    headers = ["source_col", "predicted_target", "layer", "conf", "correct"]
    sep = "+-" + "-+-".join("-" * w for w in W) + "-+"
    print(sep)
    print("| " + " | ".join(h.ljust(w) for h, w in zip(headers, W)) + " |")
    print(sep)
    for src in sorted(mapping.keys()):
        info = mapping[src]
        tgt  = info.get("target") or "NO MATCH"
        layer = info.get("layer", "—")
        conf = f"{info.get('confidence', 0.0):.4f}"
        hit  = "YES" if info.get("target") == GROUND_TRUTH.get(src) else "NO"
        cells = [src.ljust(W[0]), tgt.ljust(W[1]), layer.ljust(W[2]),
                 conf.ljust(W[3]), hit.ljust(W[4])]
        print("| " + " | ".join(cells) + " |")
    print(sep)

    # ── Accuracy report ───────────────────────────────────────────────────────
    total_gt = len(GROUND_TRUTH)
    combined_correct = sum(
        1 for src, info in mapping.items()
        if src in GROUND_TRUTH and info.get("target") == GROUND_TRUTH[src]
    )

    layer_scores = _score_by_layer(mapping, GROUND_TRUTH)
    l1_correct, l1_total = layer_scores.get("L1", (0, 0))
    l2_correct, l2_total = layer_scores.get("L2", (0, 0))
    fb_correct, fb_total = layer_scores.get("L1-fallback", (0, 0))

    print("\n=== Accuracy Report ===")
    print(f"  L1 (Valentine Jaccard):  {l1_correct}/{l1_total}")
    print(f"  L2 (Claude reasoner):    {l2_correct}/{l2_total}")
    if fb_total:
        print(f"  L1-fallback:             {fb_correct}/{fb_total}")
    print(f"  Combined:                {combined_correct}/{total_gt}")

    l2_misses = [
        src for src, info in mapping.items()
        if info.get("layer", "").startswith("L2")
        and src in GROUND_TRUTH
        and info.get("target") != GROUND_TRUTH[src]
    ]
    if l2_misses:
        print(f"\n  L2 misses: {l2_misses}")
    elif l2_total:
        print("\n  L2 resolved all escalations correctly.")

    # ── BigQuery verification ─────────────────────────────────────────────────
    print("\nVerifying BigQuery insertion…")
    bq_rows = _query_bq(run_id)
    if not bq_rows:
        print("ERROR: no rows found in BigQuery for this run_id.")
        sys.exit(1)
    l1_bq = sum(1 for r in bq_rows if r["layer"] == "L1")
    l2_bq = sum(1 for r in bq_rows if r["layer"] == "L2")
    fb_bq = sum(1 for r in bq_rows if r["layer"] == "L1-fallback")
    print(f"  BigQuery rows : {len(bq_rows)}")
    print(f"  Layer breakdown: L1={l1_bq}  L2={l2_bq}  fallback={fb_bq}")

    if combined_correct == total_gt:
        print(f"\n*** {combined_correct}/{total_gt} — Stage 3 PASSED ***")
    else:
        missed = [
            src for src in GROUND_TRUTH
            if mapping.get(src, {}).get("target") != GROUND_TRUTH[src]
        ]
        print(f"\nStage 3 PARTIAL — misses: {missed}")
