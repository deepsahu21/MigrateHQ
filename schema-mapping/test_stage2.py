"""
Stage 2 test — fully semantic renames (no lexical overlap between source and target).

Ground truth:
  order_id                    → transaction_ref
  customer_id                 → buyer_key
  order_status                → fulfillment_stage
  order_purchase_timestamp    → created_at
  order_approved_at           → confirmed_time
  order_delivered_carrier_date  → shipped_date
  order_delivered_customer_date → received_date
  order_estimated_delivery_date → promised_date

Strategy:
  - L1 (Valentine Jaccard) will fail on all 8 (no lexical overlap).
  - L2 uses semantic_match_batch() with semantically-grouped batches so the model
    can apply mutual-exclusion reasoning (e.g., carrier date vs customer date).
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import pandas as pd
from layer2_claude.reasoner import semantic_match_batch
from orchestrator import print_results_table
from valentine import valentine_match
from valentine.algorithms import JaccardDistanceMatcher

GROUND_TRUTH = {
    "order_id": "transaction_ref",
    "customer_id": "buyer_key",
    "order_status": "fulfillment_stage",
    "order_purchase_timestamp": "created_at",
    "order_approved_at": "confirmed_time",
    "order_delivered_carrier_date": "shipped_date",
    "order_delivered_customer_date": "received_date",
    "order_estimated_delivery_date": "promised_date",
}

SOURCE_CSV = "data/raw/olist_orders_dataset.csv"
TARGET_CSV = "data/manufactured/olist_orders_stage2_manufactured.csv"

L1_THRESHOLD = 0.8

# Batch definitions: (batch_name, [source_cols], [target_cols])
# Semantically related columns are grouped so Gemini reasons with mutual exclusion.
BATCHES = [
    (
        "delivery-dates",
        ["order_delivered_carrier_date", "order_delivered_customer_date"],
        ["shipped_date", "received_date"],
    ),
    (
        "order-identifiers",
        ["order_id", "customer_id"],
        ["transaction_ref", "buyer_key"],
    ),
    (
        "timestamps",
        ["order_purchase_timestamp", "order_approved_at", "order_estimated_delivery_date"],
        ["created_at", "confirmed_time", "promised_date"],
    ),
    (
        "status",
        ["order_status"],
        ["fulfillment_stage"],
    ),
]


def samples_for(df: pd.DataFrame, cols: list[str], n: int = 8) -> dict[str, list]:
    return {c: df[c].dropna().astype(str).head(n).tolist() for c in cols if c in df.columns}


def score_mapping(mapping: dict, ground_truth: dict, layer_filter=None):
    correct = total = 0
    for src, info in mapping.items():
        if src not in ground_truth:
            continue
        if layer_filter and not info["layer"].startswith(layer_filter):
            continue
        total += 1
        if info["target"] == ground_truth[src]:
            correct += 1
    return correct, total


if __name__ == "__main__":
    source_df = pd.read_csv(SOURCE_CSV).sample(n=100, random_state=42)
    target_df = pd.read_csv(TARGET_CSV).sample(n=100, random_state=99)

    source_cols = list(source_df.columns)
    target_cols = list(target_df.columns)

    print("\n=== Stage 2 Test: Semantic Renames (Batched L2) ===")
    print(f"Source cols: {source_cols}")
    print(f"Target cols: {target_cols}\n")

    # ── Layer 1: Valentine ─────────────────────────────────────────────────────
    matcher = JaccardDistanceMatcher()
    raw_matches = valentine_match(source_df, target_df, matcher)

    candidates: dict = {}
    for (src_t, tgt_t), score in raw_matches.items():
        src, tgt = src_t[1], tgt_t[1]
        candidates.setdefault(src, []).append((tgt, score))
    for src in candidates:
        candidates[src].sort(key=lambda x: x[1], reverse=True)

    final_mapping: dict = {}
    claimed_targets: set = set()
    l1_cols = []

    for src in source_cols:
        ranked = candidates.get(src, [])
        if ranked and ranked[0][1] >= L1_THRESHOLD:
            best_tgt, best_score = ranked[0]
            final_mapping[src] = {"target": best_tgt, "confidence": best_score, "layer": "L1"}
            claimed_targets.add(best_tgt)
            l1_cols.append(src)

    escalated = [s for s in source_cols if s not in final_mapping]
    print(f"L1 claimed: {l1_cols}")
    print(f"Escalated to L2: {escalated}\n")

    # ── Layer 2: Batched semantic matching ────────────────────────────────────
    l2_results: dict[str, dict] = {}

    for batch_name, src_cols, tgt_cols in BATCHES:
        # Only process source cols that actually need L2
        active_src = [s for s in src_cols if s in escalated and s not in l2_results]
        # Only offer target cols not yet claimed
        active_tgt = [t for t in tgt_cols if t not in claimed_targets]

        if not active_src:
            continue

        src_samp = samples_for(source_df, active_src)
        tgt_samp = samples_for(target_df, active_tgt)

        matches = semantic_match_batch(
            batch_name=batch_name,
            source_columns=active_src,
            target_columns=active_tgt,
            source_samples=src_samp,
            target_samples=tgt_samp,
        )

        for m in matches:
            src = m.get("source")
            tgt = m.get("target")
            conf = m.get("confidence", 0.0)
            if src and tgt and tgt in active_tgt and tgt not in claimed_targets:
                l2_results[src] = {"target": tgt, "confidence": conf, "layer": "L2"}
                claimed_targets.add(tgt)
            elif src:
                l2_results[src] = {"target": None, "confidence": 0.0, "layer": "L2-no-match"}

    # Any escalated cols not covered by a batch
    for src in escalated:
        if src not in l2_results:
            l2_results[src] = {"target": None, "confidence": 0.0, "layer": "none"}

    final_mapping.update(l2_results)

    # ── Results ───────────────────────────────────────────────────────────────
    print_results_table(final_mapping, ground_truth=GROUND_TRUTH)

    l1_correct, l1_total = score_mapping(final_mapping, GROUND_TRUTH, layer_filter="L1")
    l2_correct, l2_total = score_mapping(final_mapping, GROUND_TRUTH, layer_filter="L2")
    combined_correct, combined_total = score_mapping(final_mapping, GROUND_TRUTH)

    print("\n=== Accuracy Report ===")
    print(f"  L1 (Valentine Jaccard):  {l1_correct}/{l1_total} — resolved at threshold")
    print(f"  L2 (Gemini batched):     {l2_correct}/{l2_total} — escalated from L1")
    print(f"  Combined:                {combined_correct}/{combined_total}")

    l2_wrong = [
        src for src, info in final_mapping.items()
        if info["layer"].startswith("L2") and info["target"] != GROUND_TRUTH.get(src)
        and src in GROUND_TRUTH
    ]
    if l2_wrong:
        print(f"\n  L2 misses: {l2_wrong}")
    else:
        print("\n  L2 resolved all escalations correctly.")

    if combined_correct == combined_total == len(GROUND_TRUTH):
        print("\n  *** 8/8 — Stage 2 PASSED ***")
        print("""
## NEXT STEPS (after Stage 2 passes):

1. STAGE 3: Load BarrelSense MongoDB data from schema-mapping/data/barrelSense/
   - Pick two collections for testing
   - Run L1 + L2 orchestration against them
   - Report: accuracy, layer breakdown, confidence distribution
   - Target: >= 80% accuracy on real production-like schema

2. BUILD schema-mapping/src/layer3_human_review/ (after Stage 3 passes)
   - Create a queue structure for low-confidence matches
   - Store in BigQuery table: human_review_queue
   - Columns: run_id, source_column, target_column, l1_confidence, l2_confidence, reasoning, status (pending/approved/rejected)

3. REFACTOR orchestrator.py to push unresolved/low-confidence pairs to human_review_queue in BigQuery
""")
