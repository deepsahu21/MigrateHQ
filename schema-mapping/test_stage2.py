"""
Stage 2 test — semantic renames (no lexical overlap between source and target).

Ground truth:
  order_id                      → transaction_ref
  customer_id                   → client_code
  order_status                  → fulfillment_stage
  order_purchase_timestamp      → initiated_at
  order_approved_at             → payment_confirmed_time
  order_delivered_carrier_date  → carrier_handoff_ts
  order_delivered_customer_date → last_mile_completion_dt
  order_estimated_delivery_date → promised_date

Strategy:
  - L1 (Valentine Jaccard) will fail on all 8 (no lexical overlap).
  - L2 uses semantic_match_batch() with semantically-grouped batches so the model
    can apply mutual-exclusion reasoning (e.g., carrier ts vs customer delivery dt).
  - Results are dual-written to BigQuery + Supabase (client label: olist_orders_stage2_man).
"""
import sys
import os
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import pandas as pd
from layer2_claude.reasoner import semantic_match_batch
from orchestrator import print_results_table
from bigquery_loader import push_mapping_to_bigquery, write_to_supabase
from valentine import valentine_match
from valentine.algorithms import JaccardDistanceMatcher

GROUND_TRUTH = {
    "order_id":                      "transaction_ref",
    "customer_id":                   "client_code",
    "order_status":                  "fulfillment_stage",
    "order_purchase_timestamp":      "initiated_at",
    "order_approved_at":             "payment_confirmed_time",
    "order_delivered_carrier_date":  "carrier_handoff_ts",
    "order_delivered_customer_date": "last_mile_completion_dt",
    "order_estimated_delivery_date": "promised_date",
}

SOURCE_CSV = "data/raw/olist_orders_dataset.csv"
TARGET_CSV = "data/manufactured/olist_orders_stage2_man.csv"
CLIENT_LABEL = "olist_orders_stage2_man"

L1_THRESHOLD = 0.8

BATCHES = [
    (
        "delivery-dates",
        ["order_delivered_carrier_date", "order_delivered_customer_date"],
        ["carrier_handoff_ts", "last_mile_completion_dt"],
    ),
    (
        "order-identifiers",
        ["order_id", "customer_id"],
        ["transaction_ref", "client_code"],
    ),
    (
        "timestamps",
        ["order_purchase_timestamp", "order_approved_at", "order_estimated_delivery_date"],
        ["initiated_at", "payment_confirmed_time", "promised_date"],
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
    print(f"Source : {SOURCE_CSV}")
    print(f"Target : {TARGET_CSV}")
    print(f"Client : {CLIENT_LABEL}")
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
        active_src = [s for s in src_cols if s in escalated and s not in l2_results]
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

    for src in escalated:
        if src not in l2_results:
            l2_results[src] = {"target": None, "confidence": 0.0, "layer": "none"}

    final_mapping.update(l2_results)

    # ── Results table ─────────────────────────────────────────────────────────
    print_results_table(final_mapping, ground_truth=GROUND_TRUTH)

    # ── Per-column layer attribution ──────────────────────────────────────────
    print("\n=== Per-Column Layer Attribution ===\n")
    W = [35, 28, 14, 8, 8]
    headers = ["source_col", "predicted_target", "layer", "conf", "correct"]
    sep = "+-" + "-+-".join("-" * w for w in W) + "-+"
    print(sep)
    print("| " + " | ".join(h.ljust(w) for h, w in zip(headers, W)) + " |")
    print(sep)
    for src in sorted(final_mapping.keys()):
        info = final_mapping[src]
        tgt  = info.get("target") or "NO MATCH"
        layer = info.get("layer", "—")
        conf = f"{info.get('confidence', 0.0):.4f}"
        hit  = "YES" if info.get("target") == GROUND_TRUTH.get(src) else "NO"
        cells = [src.ljust(W[0]), tgt.ljust(W[1]), layer.ljust(W[2]),
                 conf.ljust(W[3]), hit.ljust(W[4])]
        print("| " + " | ".join(cells) + " |")
    print(sep)

    # ── Accuracy report ───────────────────────────────────────────────────────
    l1_correct, l1_total = score_mapping(final_mapping, GROUND_TRUTH, layer_filter="L1")
    l2_correct, l2_total = score_mapping(final_mapping, GROUND_TRUTH, layer_filter="L2")
    combined_correct, combined_total = score_mapping(final_mapping, GROUND_TRUTH)

    print("\n=== Accuracy Report ===")
    print(f"  L1 (Valentine Jaccard):  {l1_correct}/{l1_total}")
    print(f"  L2 (Gemini batched):     {l2_correct}/{l2_total}")
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

    # ── Dual-write to BigQuery + Supabase ─────────────────────────────────────
    print(f"\nWriting run to BigQuery + Supabase (client={CLIENT_LABEL})…")
    run_id = str(uuid.uuid4())
    run_ts = datetime.now(timezone.utc).isoformat()

    run_id = push_mapping_to_bigquery(
        mapping_result=final_mapping,
        source_dataset="olist_orders_dataset.csv",
        target_dataset=TARGET_CSV.split("/")[-1],
        notes="stage2 semantic renames",
        run_id=run_id,
    )

    write_to_supabase(
        mapping_result=final_mapping,
        source_dataset=CLIENT_LABEL,
        run_id=run_id,
        run_timestamp=run_ts,
    )

    print(f"  run_id : {run_id}")

    if combined_correct == combined_total == len(GROUND_TRUTH):
        print(f"\n*** {combined_correct}/{combined_total} — Stage 2 PASSED ***")
    else:
        missed = [s for s in GROUND_TRUTH if final_mapping.get(s, {}).get("target") != GROUND_TRUTH[s]]
        print(f"\nStage 2 PARTIAL — misses: {missed}")
