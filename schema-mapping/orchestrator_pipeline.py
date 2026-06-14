"""
High-level pipeline: CSV files → orchestrator → BigQuery.

Entry point: run_mapping_pipeline()
"""
import os
import sys
import logging
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import pandas as pd

from orchestrator import run_orchestrator
from bigquery_loader import push_mapping_to_bigquery, write_to_supabase

logger = logging.getLogger(__name__)


def run_mapping_pipeline(
    source_csv_path: str,
    target_csv_path: str,
    dataset_name: str,
    notes: str = None,
    sample_size: int = None,
    random_state: int = 42,
    source_csv_kwargs: dict = None,
    target_csv_kwargs: dict = None,
    client_label: str = None,
) -> dict:
    """
    Load two CSVs, run L1→L2 schema mapping, push results to BigQuery.

    Args:
        source_csv_path:   path to source schema CSV
        target_csv_path:   path to target schema CSV
        dataset_name:      human label for this migration (e.g. "olist_stage1")
        notes:             optional annotation stored in mapping_runs
        sample_size:       if set, sample this many rows from each CSV before matching
        random_state:      reproducible sampling seed
        source_csv_kwargs: extra kwargs forwarded to pd.read_csv for source (e.g. {"skiprows": 1})
        target_csv_kwargs: extra kwargs forwarded to pd.read_csv for target (e.g. {"skiprows": 1})

    Returns:
        {
            "status":         "success" | "error",
            "run_id":         str (on success),
            "total_columns":  int,
            "l1_count":       int,
            "l2_count":       int,
            "fallback_count": int,
            "error":          str (on error only),
        }
    """
    # ── Load ─────────────────────────────────────────────────────────────────
    try:
        source_df = pd.read_csv(source_csv_path, **(source_csv_kwargs or {}))
        target_df = pd.read_csv(target_csv_path, **(target_csv_kwargs or {}))
    except Exception as exc:
        logger.error("Failed to load CSVs: %s", exc)
        return {"status": "error", "error": f"CSV load failed: {exc}"}

    if sample_size:
        source_df = source_df.sample(n=min(sample_size, len(source_df)), random_state=random_state)
        target_df = target_df.sample(n=min(sample_size, len(target_df)), random_state=random_state)

    logger.info(
        "Loaded source=%s (%d rows, %d cols), target=%s (%d rows, %d cols)",
        os.path.basename(source_csv_path), len(source_df), len(source_df.columns),
        os.path.basename(target_csv_path), len(target_df), len(target_df.columns),
    )

    # ── Orchestrate ───────────────────────────────────────────────────────────
    try:
        mapping = run_orchestrator(source_df, target_df)
    except Exception as exc:
        logger.error("Orchestrator failed: %s", exc)
        return {"status": "error", "error": f"Orchestrator failed: {exc}"}

    l1_count = sum(1 for v in mapping.values() if v.get("layer") == "L1")
    l2_count = sum(1 for v in mapping.values() if v.get("layer") == "L2")
    fallback_count = sum(1 for v in mapping.values() if v.get("layer") == "L1-fallback")

    # Use the same label for both BigQuery and Supabase so source_dataset
    # is consistent across both stores.
    source_label = client_label or os.path.basename(source_csv_path)

    # ── Push to BigQuery ──────────────────────────────────────────────────────
    try:
        run_id = push_mapping_to_bigquery(
            mapping_result=mapping,
            source_dataset=source_label,
            target_dataset=os.path.basename(target_csv_path),
            notes=notes or f"pipeline:{dataset_name}",
        )
    except Exception as exc:
        logger.error("BigQuery push failed: %s", exc)
        return {
            "status": "error",
            "error": f"BigQuery push failed: {exc}",
            "total_columns": len(mapping),
            "l1_count": l1_count,
            "l2_count": l2_count,
            "fallback_count": fallback_count,
        }

    # ── Mirror to Supabase (non-fatal) ────────────────────────────────────────
    write_to_supabase(
        mapping_result=mapping,
        source_dataset=source_label,
        run_id=run_id,
        run_timestamp=datetime.now(timezone.utc).isoformat(),
    )

    logger.info(
        "Pipeline complete: run_id=%s  total=%d  L1=%d  L2=%d  fallback=%d",
        run_id, len(mapping), l1_count, l2_count, fallback_count,
    )

    return {
        "status": "success",
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source_dataset": os.path.basename(source_csv_path),
        "target_dataset": os.path.basename(target_csv_path),
        "total_columns": len(mapping),
        "l1_count": l1_count,
        "l2_count": l2_count,
        "fallback_count": fallback_count,
        "mapping": mapping,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    result = run_mapping_pipeline(
        source_csv_path="data/raw/olist_orders_dataset.csv",
        target_csv_path="data/manufactured/olist_orders_stage1_man.csv",
        dataset_name="olist_stage1",
        sample_size=100,
    )

    print("\n=== Pipeline Result ===")
    for k, v in result.items():
        print(f"  {k}: {v}")
