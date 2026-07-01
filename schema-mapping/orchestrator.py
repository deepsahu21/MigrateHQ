"""
L1 → L2 orchestration pipeline for schema mapping.

Architecture:
  1. Layer 1 (Valentine JaccardDistanceMatcher) runs first on all source/target pairs.
  2. Matches with L1 confidence >= L1_THRESHOLD are accepted as-is.
  3. Remaining columns are grouped by semantic category (timestamps, identifiers,
     status, other) and escalated to Layer 2 (Gemini). Each category group is
     further chunked into sub-batches of at most MAX_BATCH_SIZE columns so no
     single LLM call receives an oversized, unfocused prompt. One call is made
     per sub-batch with full mutual-exclusion reasoning across its columns.
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

from layer2_claude.reasoner import semantic_match_batch

# ── Configurable thresholds ────────────────────────────────────────────────────
L1_THRESHOLD = 0.8
MAX_BATCH_SIZE = 6  # max source columns per semantic_match_batch() call


def _group_by_category(cols: list) -> dict:
    """
    Group column names into semantic buckets for batched L2 matching.

    Splits each name on _, -, and space, then tests tokens against known word
    sets. Priority: timestamps → identifiers → status → other.
    Returns only non-empty groups in iteration order.
    """
    date_words   = {"date", "time", "ts", "at", "timestamp", "dt", "day", "when"}
    id_words     = {"id", "ref", "code", "num", "number", "key", "uuid"}
    status_words = {"status", "state", "stage", "type", "flag", "mode"}
    groups: dict = {"timestamps": [], "identifiers": [], "status": [], "other": []}
    for col in cols:
        tokens = set(col.lower().replace("-", "_").replace(" ", "_").split("_"))
        if tokens & date_words:
            groups["timestamps"].append(col)
        elif tokens & id_words:
            groups["identifiers"].append(col)
        elif tokens & status_words:
            groups["status"].append(col)
        else:
            groups["other"].append(col)
    return {k: v for k, v in groups.items() if v}


def _apply_l1_fallback(
    src: str,
    src_sample_list: list,
    candidates: dict,
    claimed_targets: set,
    target_df,
    final_mapping: dict,
) -> None:
    """Write the best unclaimed L1 candidate as an L1-fallback entry, or 'none'."""
    ranked = candidates.get(src, [])
    for best_tgt, best_score in ranked:
        if best_tgt not in claimed_targets:
            tgt_fb = (
                target_df[best_tgt].dropna().astype(str).head(5).tolist()
                if best_tgt in target_df.columns else []
            )
            final_mapping[src] = {
                "target":         best_tgt,
                "confidence":     best_score,
                "layer":          "L1-fallback",
                "source_samples": src_sample_list,
                "target_samples": tgt_fb,
            }
            claimed_targets.add(best_tgt)
            return
    final_mapping[src] = {
        "target":         None,
        "confidence":     0.0,
        "layer":          "none",
        "source_samples": src_sample_list,
        "target_samples": [],
    }


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
            src_samples = source_df[src].dropna().astype(str).head(5).tolist()
            tgt_samples = target_df[best_tgt].dropna().astype(str).head(5).tolist() if best_tgt in target_df.columns else []
            final_mapping[src] = {
                "target": best_tgt,
                "confidence": best_score,
                "layer": "L1",
                "source_samples": src_samples,
                "target_samples": tgt_samples,
            }
            claimed_targets.add(best_tgt)
        else:
            escalate.append(src)

    # ── Layer 2: Batched semantic matching ────────────────────────────────────
    # Group escalated columns by semantic category (timestamps, identifiers,
    # status, other), then chunk each group into sub-batches of at most
    # MAX_BATCH_SIZE columns. Each sub-batch is one semantic_match_batch()
    # call with mutual-exclusion reasoning across its columns. claimed_targets
    # is carried forward so targets cannot be double-assigned across sub-batches.
    batch_groups = _group_by_category(escalate)

    for batch_name, src_group in batch_groups.items():
        sub_batches = [
            src_group[i:i + MAX_BATCH_SIZE]
            for i in range(0, len(src_group), MAX_BATCH_SIZE)
        ]
        for sub_idx, sub_group in enumerate(sub_batches):
            sub_name = (
                batch_name if len(sub_batches) == 1
                else f"{batch_name}_{sub_idx + 1}"
            )

            available_targets = [t for t in target_cols if t not in claimed_targets]
            if not available_targets:
                for src in sub_group:
                    final_mapping[src] = {
                        "target": None, "confidence": 0.0, "layer": "none",
                        "source_samples": source_df[src].dropna().astype(str).head(5).tolist(),
                        "target_samples": [],
                    }
                continue

            src_samples = {
                src: source_df[src].dropna().astype(str).head(5).tolist()
                for src in sub_group
            }
            tgt_samples = {
                t: target_df[t].dropna().astype(str).head(5).tolist()
                for t in available_targets
            }

            time.sleep(1)  # rate-limit between every sub-batch call
            matches = semantic_match_batch(
                batch_name=sub_name,
                source_columns=sub_group,
                target_columns=available_targets,
                source_samples=src_samples,
                target_samples=tgt_samples,
            )

            resolved: set = set()
            for m in matches:
                src = m.get("source")
                tgt = m.get("target")
                conf = float(m.get("confidence") or 0.0)
                if src not in sub_group:
                    continue  # model hallucinated an unknown source column — skip
                if tgt and tgt in available_targets and tgt not in claimed_targets:
                    final_mapping[src] = {
                        "target":         tgt,
                        "confidence":     conf,
                        "layer":          "L2",
                        "source_samples": src_samples.get(src, []),
                        "target_samples": tgt_samples.get(tgt, []),
                    }
                    claimed_targets.add(tgt)
                else:
                    # L2 returned null or a target already claimed — L1-fallback
                    _apply_l1_fallback(src, src_samples.get(src, []), candidates, claimed_targets, target_df, final_mapping)
                resolved.add(src)

            # Safety: apply fallback for any source columns the model omitted entirely
            for src in sub_group:
                if src not in resolved:
                    _apply_l1_fallback(src, src_samples.get(src, []), candidates, claimed_targets, target_df, final_mapping)

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
