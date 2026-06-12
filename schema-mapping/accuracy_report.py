"""
Generates an accuracy report from a run_mapping_pipeline() output dict.
"""
import os
import json
import logging
from datetime import datetime, timezone
from statistics import mean

logger = logging.getLogger(__name__)

FLAGGED_THRESHOLD = 0.75
REPORTS_DIR = os.path.join(os.path.dirname(__file__), "reports")


def generate_accuracy_report(pipeline_output: dict) -> dict:
    """
    Compute accuracy metrics from a pipeline run and save a JSON report.

    Args:
        pipeline_output: dict returned by run_mapping_pipeline()

    Returns:
        accuracy report dict
    """
    if pipeline_output.get("status") != "success":
        raise ValueError(f"Pipeline did not succeed: {pipeline_output.get('error')}")

    mapping: dict = pipeline_output.get("mapping", {})

    confidences = [v["confidence"] for v in mapping.values() if v.get("confidence") is not None]
    flagged_columns = [
        src for src, v in mapping.items() if (v.get("confidence") or 0.0) < FLAGGED_THRESHOLD
    ]

    total = len(mapping)
    l1_count = pipeline_output.get("l1_count", 0)
    l2_count = pipeline_output.get("l2_count", 0)
    fallback_count = pipeline_output.get("fallback_count", 0)
    matched = l1_count + l2_count + fallback_count

    report = {
        "run_id": pipeline_output["run_id"],
        "timestamp": pipeline_output.get("timestamp", datetime.now(timezone.utc).isoformat()),
        "source_dataset": pipeline_output.get("source_dataset", ""),
        "target_dataset": pipeline_output.get("target_dataset", ""),
        "total_columns": total,
        "l1_count": l1_count,
        "l2_count": l2_count,
        "fallback_count": fallback_count,
        "l1_accuracy_pct": round(l1_count / total * 100, 1) if total else 0.0,
        "l2_accuracy_pct": round(l2_count / total * 100, 1) if total else 0.0,
        "combined_accuracy_pct": round(matched / total * 100, 1) if total else 0.0,
        "fallback_rate_pct": round(fallback_count / total * 100, 1) if total else 0.0,
        "confidence_mean": round(mean(confidences), 2) if confidences else 0.0,
        "confidence_min": round(min(confidences), 2) if confidences else 0.0,
        "confidence_max": round(max(confidences), 2) if confidences else 0.0,
        "flagged_for_review_count": len(flagged_columns),
        "flagged_columns": flagged_columns,
    }

    os.makedirs(REPORTS_DIR, exist_ok=True)
    report_path = os.path.join(REPORTS_DIR, f"{pipeline_output['run_id']}_report.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    logger.info("Accuracy report saved: %s", report_path)

    return report


def print_accuracy_report(report: dict) -> None:
    """Print a formatted accuracy report summary to stdout."""
    w = 55
    print("\n" + "=" * w)
    print("  MigrateHQ Accuracy Report")
    print("=" * w)
    print(f"  run_id         : {report['run_id']}")
    print(f"  timestamp      : {report['timestamp']}")
    print(f"  source         : {report['source_dataset']}")
    print(f"  target         : {report['target_dataset']}")
    print("-" * w)
    print(f"  total columns  : {report['total_columns']}")
    print(f"  L1 matches     : {report['l1_count']}  ({report['l1_accuracy_pct']}%)")
    print(f"  L2 matches     : {report['l2_count']}  ({report['l2_accuracy_pct']}%)")
    print(f"  fallback       : {report['fallback_count']}  ({report['fallback_rate_pct']}%)")
    print(f"  combined acc   : {report['combined_accuracy_pct']}%")
    print("-" * w)
    print(f"  confidence avg : {report['confidence_mean']}")
    print(f"  confidence min : {report['confidence_min']}")
    print(f"  confidence max : {report['confidence_max']}")
    print(f"  flagged        : {report['flagged_for_review_count']} column(s)")
    if report["flagged_columns"]:
        for col in report["flagged_columns"]:
            print(f"    - {col}")
    print("=" * w + "\n")


if __name__ == "__main__":
    import sys

    sys.path.insert(0, os.path.dirname(__file__))
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    # Smoke-test against Stage 1 mock data matching known ground truth
    mock_pipeline_output = {
        "status": "success",
        "run_id": "smoke-test-stage1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source_dataset": "olist_orders_dataset.csv",
        "target_dataset": "olist_orders_stage1_man.csv",
        "total_columns": 8,
        "l1_count": 8,
        "l2_count": 0,
        "fallback_count": 0,
        "mapping": {
            "order_id":                      {"target": "ord_number",              "confidence": 0.95, "layer": "L1"},
            "customer_id":                   {"target": "cust_ID",                 "confidence": 0.91, "layer": "L1"},
            "order_status":                  {"target": "order_state",             "confidence": 0.88, "layer": "L1"},
            "order_purchase_timestamp":      {"target": "purchase_ts",             "confidence": 0.84, "layer": "L1"},
            "order_approved_at":             {"target": "approved_at",             "confidence": 0.93, "layer": "L1"},
            "order_delivered_carrier_date":  {"target": "carrier_delivery_date",   "confidence": 0.87, "layer": "L1"},
            "order_delivered_customer_date": {"target": "customer_delivery_date",  "confidence": 0.89, "layer": "L1"},
            "order_estimated_delivery_date": {"target": "estimated_delivery_date", "confidence": 0.92, "layer": "L1"},
        },
    }

    report = generate_accuracy_report(mock_pipeline_output)
    print_accuracy_report(report)
    print(f"Report saved to: reports/{mock_pipeline_output['run_id']}_report.json")
