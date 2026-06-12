"""
L1 → L2 orchestration pipeline for schema mapping.

Architecture:
  1. Layer 1 (Valentine JaccardDistanceMatcher) runs first on all source/target pairs.
  2. Matches with L1 confidence >= L1_THRESHOLD are accepted as-is.
  3. Remaining columns are escalated to Layer 2 (Gemini) which picks the best
     target from unclaimed candidates in a single LLM call per column.
  4. Returns a merged mapping with per-column provenance (L1 or L2).
"""
import sys
import os
import time
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import pandas as pd
from valentine import valentine_match
from valentine.algorithms import JaccardDistanceMatcher

from layer2_claude.reasoner import reason_best_match

# ── Configurable threshold ─────────────────────────────────────────────────────
L1_THRESHOLD = 0.8


def run_orchestrator(
    source_df: pd.DataFrame,
    target_df: pd.DataFrame,
    threshold: float = L1_THRESHOLD,
) -> dict:
    """
    Run L1→L2 schema matching pipeline.

    Returns:
        dict mapping source_col → {"target": str|None, "confidence": float, "layer": str}
        layer values: "L1", "L2", "L1-fallback", "none"
    """
    source_cols = list(source_df.columns)
    target_cols = list(target_df.columns)

    # ── Layer 1: Valentine ─────────────────────────────────────────────────────
    matcher = JaccardDistanceMatcher()
    raw_matches = valentine_match(source_df, target_df, matcher)

    # Build ranked candidate list per source col: {src: [(tgt, score), ...]}
    candidates = {}
    for (src_tuple, tgt_tuple), score in raw_matches.items():
        src, tgt = src_tuple[1], tgt_tuple[1]
        candidates.setdefault(src, []).append((tgt, score))
    for src in candidates:
        candidates[src].sort(key=lambda x: x[1], reverse=True)

    # Ensure every source col has at least a score-0 entry for every target col,
    # so L1-fallback always has something to reference when L2 fails.
    seen_tgts = {src: {tgt for tgt, _ in v} for src, v in candidates.items()}
    for src in source_cols:
        for tgt in target_cols:
            if tgt not in seen_tgts.get(src, set()):
                candidates.setdefault(src, []).append((tgt, 0.0))

    # Accept high-confidence L1 matches; queue the rest for L2
    final_mapping = {}
    claimed_targets = set()
    escalate: list = []

    for src in source_cols:
        ranked = candidates.get(src, [])
        if ranked and ranked[0][1] >= threshold:
            best_tgt, best_score = ranked[0]
            final_mapping[src] = {"target": best_tgt, "confidence": best_score, "layer": "L1"}
            claimed_targets.add(best_tgt)
        else:
            escalate.append(src)

    # ── Layer 2: LLM Reasoner ─────────────────────────────────────────────────
    for src in escalate:
        available_targets = [t for t in target_cols if t not in claimed_targets]
        if not available_targets:
            final_mapping[src] = {"target": None, "confidence": 0.0, "layer": "none"}
            continue

        src_samples = source_df[src].dropna().astype(str).head(5).tolist()
        target_samples_map = {
            t: target_df[t].dropna().astype(str).head(5).tolist()
            for t in available_targets
        }

        time.sleep(1)  # avoid rate-limiting sequential LLM calls
        result = reason_best_match(src, available_targets, src_samples, target_samples_map)
        matched_tgt = result.get("best_match")

        if matched_tgt and matched_tgt in available_targets:
            final_mapping[src] = {
                "target": matched_tgt,
                "confidence": result.get("confidence", 0.0),
                "layer": "L2",
            }
            claimed_targets.add(matched_tgt)
        else:
            # L2 also failed — fall back to best unclaimed L1 candidate
            ranked = candidates.get(src, [])
            for best_tgt, best_score in ranked:
                if best_tgt not in claimed_targets:
                    final_mapping[src] = {
                        "target": best_tgt,
                        "confidence": best_score,
                        "layer": "L1-fallback",
                    }
                    claimed_targets.add(best_tgt)
                    break
            else:
                final_mapping[src] = {"target": None, "confidence": 0.0, "layer": "none"}

    return final_mapping


def print_results_table(final_mapping: dict, ground_truth: Optional[dict] = None) -> None:
    W = [32, 22, 10, 14]
    headers = ["source col", "predicted target", "conf", "layer"]
    if ground_truth:
        W.append(8)
        headers.append("correct?")

    sep = "+-" + "-+-".join("-" * w for w in W) + "-+"
    hdr = "| " + " | ".join(h.ljust(w) for h, w in zip(headers, W)) + " |"

    print(sep)
    print(hdr)
    print(sep)

    for src, info in final_mapping.items():
        tgt = info["target"] or "NO MATCH"
        conf = f"{info['confidence']:.4f}"
        layer = info["layer"]
        cells = [
            src.ljust(W[0]),
            tgt.ljust(W[1]),
            conf.ljust(W[2]),
            layer.ljust(W[3]),
        ]
        if ground_truth:
            hit = info["target"] == ground_truth.get(src)
            cells.append(("YES" if hit else "NO").ljust(W[4]))
        print("| " + " | ".join(cells) + " |")

    print(sep)


if __name__ == "__main__":
    source_df = pd.read_csv("data/raw/olist_orders_dataset.csv").sample(n=100, random_state=42)
    target_df = pd.read_csv("data/manufactured/olist_orders_stage3_man.csv").sample(n=100, random_state=99)

    print("\n=== Orchestrator: Stage 3 ===\n")
    mapping = run_orchestrator(source_df, target_df)
    print_results_table(mapping)
