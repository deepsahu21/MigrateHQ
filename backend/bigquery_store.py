"""
BigQuery data layer — used when Supabase is unreachable.

Schema mirrors migrate_bigquery_to_supabase.py (mapping_runs, mapping_results).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from google.cloud import bigquery

BQ_PROJECT = "project-bf89f8dc-434b-4108-be6"
BQ_DATASET = "migratehq"
RUNS_TABLE = f"{BQ_PROJECT}.{BQ_DATASET}.mapping_runs"
RESULTS_TABLE = f"{BQ_PROJECT}.{BQ_DATASET}.mapping_results"

_bq: Optional[bigquery.Client] = None


def _client() -> bigquery.Client:
    global _bq
    if _bq is None:
        _bq = bigquery.Client(project=BQ_PROJECT)
    return _bq


def _tenant_clause(tenant: str, param: str = "tenant") -> str:
    if tenant.lower() == "migratehq":
        return "TRUE"
    return f"LOWER(source_dataset) LIKE CONCAT('%', LOWER(@{param}), '%')"


def _rows(sql: str, params: Optional[dict] = None) -> list[dict]:
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(k, "STRING", v)
            for k, v in (params or {}).items()
        ]
    )
    return [dict(row) for row in _client().query(sql, job_config=job_config).result()]


def get_source_datasets(tenant: str) -> List[str]:
    sql = f"""
        SELECT DISTINCT source_dataset
        FROM `{RUNS_TABLE}`
        WHERE {_tenant_clause(tenant)}
        ORDER BY source_dataset
    """
    return [r["source_dataset"] for r in _rows(sql, {"tenant": tenant})]


def get_overview(tenant: str) -> dict:
    sources = get_source_datasets(tenant)
    if not sources:
        return {
            "total_clients": 0,
            "overall_accuracy_pct": 0.0,
            "total_columns_mapped": 0,
            "flagged_count": 0,
        }

    sql = f"""
        WITH latest AS (
          SELECT * EXCEPT(rn) FROM (
            SELECT *, ROW_NUMBER() OVER (
              PARTITION BY source_dataset ORDER BY run_timestamp DESC
            ) AS rn
            FROM `{RUNS_TABLE}`
            WHERE {_tenant_clause(tenant)}
          )
          WHERE rn = 1
        )
        SELECT
          COUNT(*) AS total_clients,
          SUM(total_columns) AS total_columns_mapped,
          SUM(l1_count + l2_count) AS total_matched
        FROM latest
    """
    summary = _rows(sql, {"tenant": tenant})[0]
    total_cols = summary["total_columns_mapped"] or 0
    total_matched = summary["total_matched"] or 0
    accuracy = round(total_matched / total_cols * 100, 1) if total_cols else 0.0

    flagged_sql = f"""
        WITH latest AS (
          SELECT run_id FROM (
            SELECT run_id, source_dataset,
              ROW_NUMBER() OVER (PARTITION BY source_dataset ORDER BY run_timestamp DESC) AS rn
            FROM `{RUNS_TABLE}`
            WHERE {_tenant_clause(tenant)}
          )
          WHERE rn = 1
        )
        SELECT COUNT(*) AS flagged
        FROM `{RESULTS_TABLE}` r
        JOIN latest l ON r.run_id = l.run_id
        WHERE r.confidence < 0.75
    """
    flagged = _rows(flagged_sql, {"tenant": tenant})[0]["flagged"] or 0

    return {
        "total_clients": summary["total_clients"] or 0,
        "overall_accuracy_pct": accuracy,
        "total_columns_mapped": total_cols,
        "flagged_count": flagged,
    }


def get_clients(tenant: str) -> list[dict]:
    sql = f"""
        WITH runs AS (
          SELECT
            source_dataset,
            run_id,
            run_timestamp AS created_at,
            total_columns,
            l1_count,
            l2_count,
            fallback_count,
            ROW_NUMBER() OVER (PARTITION BY source_dataset ORDER BY run_timestamp DESC) AS rn,
            COUNT(*) OVER (PARTITION BY source_dataset) AS total_runs
          FROM `{RUNS_TABLE}`
          WHERE {_tenant_clause(tenant)}
        ),
        latest AS (
          SELECT * FROM runs WHERE rn = 1
        ),
        flagged AS (
          SELECT r.run_id, COUNT(*) AS flagged_count
          FROM `{RESULTS_TABLE}` r
          JOIN latest l ON r.run_id = l.run_id
          WHERE r.confidence < 0.75
          GROUP BY r.run_id
        )
        SELECT
          l.source_dataset AS client_name,
          l.created_at AS last_run_at,
          l.total_runs,
          l.total_columns,
          l.l1_count,
          l.l2_count,
          l.fallback_count,
          ROUND(SAFE_DIVIDE(l.l1_count + l.l2_count, l.total_columns) * 100, 1) AS latest_accuracy_pct,
          COALESCE(f.flagged_count, 0) AS flagged_count
        FROM latest l
        LEFT JOIN flagged f ON l.run_id = f.run_id
        ORDER BY l.source_dataset
    """
    return _rows(sql, {"tenant": tenant})


def get_mappings(client_name: str) -> list[dict]:
    sql = f"""
        WITH latest AS (
          SELECT run_id
          FROM `{RUNS_TABLE}`
          WHERE source_dataset = @client_name
          ORDER BY run_timestamp DESC
          LIMIT 1
        )
        SELECT
          r.source_column,
          r.target_column,
          r.confidence,
          r.layer,
          r.correct,
          (r.confidence < 0.75) AS flagged_for_review
        FROM `{RESULTS_TABLE}` r
        JOIN latest l ON r.run_id = l.run_id
        ORDER BY r.source_column
    """
    rows = _rows(sql, {"client_name": client_name})
    return [
        {
            "source_column": r["source_column"],
            "target_column": r["target_column"],
            "confidence": float(r["confidence"]) if r["confidence"] is not None else 0.0,
            "layer": r["layer"],
            "correct": r["correct"],
            "flagged_for_review": bool(r["flagged_for_review"]),
        }
        for r in rows
    ]


def get_runs(client_name: str) -> list[dict]:
    sql = f"""
        SELECT
          run_id,
          run_timestamp AS created_at,
          total_columns,
          l1_count,
          l2_count,
          fallback_count,
          ROUND(SAFE_DIVIDE(l1_count + l2_count, total_columns) * 100, 1) AS accuracy_pct,
          'completed' AS status
        FROM `{RUNS_TABLE}`
        WHERE source_dataset = @client_name
        ORDER BY run_timestamp DESC
    """
    return _rows(sql, {"client_name": client_name})


def get_activity(tenant: str) -> list[dict]:
    sql = f"""
        SELECT
          run_id,
          source_dataset,
          run_timestamp AS last_run_at,
          total_columns,
          l1_count,
          l2_count,
          ROUND(SAFE_DIVIDE(l1_count + l2_count, total_columns) * 100, 1) AS accuracy_pct
        FROM `{RUNS_TABLE}`
        WHERE {_tenant_clause(tenant)}
        ORDER BY run_timestamp DESC
        LIMIT 10
    """
    return _rows(sql, {"tenant": tenant})


def push_approved_result(row: dict) -> None:
    """
    Write a single approved mapping_result row to BigQuery.
    Called by the /approve endpoint for records that were held in pending_review.
    The caller is responsible for ensuring this is only called when the row's
    previous status was 'pending_review' to avoid duplicate BQ writes.
    """
    result_row = {
        "run_id":        row["run_id"],
        "source_column": row["source_column"],
        "target_column": row.get("target_column") or "",
        "confidence":    float(row["confidence"]) if row.get("confidence") is not None else 0.0,
        "layer":         row.get("layer") or "",
        "correct":       row.get("correct"),
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
    }
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
    )
    _client().load_table_from_json([result_row], RESULTS_TABLE, job_config=job_config).result()


def get_mapping_row(client_name: str, source_column: str) -> Optional[dict]:
    sql = f"""
        WITH latest AS (
          SELECT run_id
          FROM `{RUNS_TABLE}`
          WHERE source_dataset = @client_name
          ORDER BY run_timestamp DESC
          LIMIT 1
        )
        SELECT r.*
        FROM `{RESULTS_TABLE}` r
        JOIN latest l ON r.run_id = l.run_id
        WHERE r.source_column = @source_column
        LIMIT 1
    """
    rows = _rows(sql, {"client_name": client_name, "source_column": source_column})
    return rows[0] if rows else None
