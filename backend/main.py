"""
MigrateHQ FastAPI backend — Supabase data layer with BigQuery fallback.

Run with: uvicorn main:app --reload --port 8000
Auth: SUPABASE_URL + SUPABASE_SECRET_KEY in backend/.env
"""
import logging
import os
import re
import sys
import tempfile
import time
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client

import bigquery_store

# Make schema-mapping importable from the backend process.
_SCHEMA_MAPPING_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "schema-mapping")
if _SCHEMA_MAPPING_DIR not in sys.path:
    sys.path.insert(0, _SCHEMA_MAPPING_DIR)
from orchestrator_pipeline import run_mapping_pipeline  # noqa: E402

_MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # 500 MB per file

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

logger = logging.getLogger("migratehq")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]

app = FastAPI(title="MigrateHQ API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Single shared client (httpx connection pool under the hood)
sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def _supabase_reachable() -> bool:
    try:
        sb.table("clients").select("id").limit(1).execute()
        return True
    except Exception:
        return False


USE_BIGQUERY = not _supabase_reachable()
if USE_BIGQUERY:
    logger.warning(
        "Supabase unreachable — serving data from BigQuery (%s)",
        bigquery_store.RUNS_TABLE,
    )
else:
    logger.info("Connected to Supabase at %s", SUPABASE_URL)

# Load known tenants once at startup for lightweight header validation.
# In BigQuery-only mode, fall back to the seed values from schema.sql.
_KNOWN_TENANTS: set = set()
if not USE_BIGQUERY:
    try:
        _tenant_rows = sb.table("tenants").select("name").execute()
        _KNOWN_TENANTS = {r["name"].lower() for r in _tenant_rows.data}
        logger.info("Known tenants loaded: %s", _KNOWN_TENANTS)
    except Exception as _e:
        logger.warning("Could not load tenants at startup (%s) — header validation disabled", _e)
else:
    _KNOWN_TENANTS = {"migratehq", "olist"}


# ── helpers ───────────────────────────────────────────────────────────────────

def _validate_tenant(tenant: str) -> None:
    """
    Raise 404 if the tenant name is not in the set loaded at startup.
    Guards every endpoint against spoofed or mistyped X-Tenant headers.
    No-ops if _KNOWN_TENANTS is empty (startup load failed) to avoid locking
    everyone out due to a transient DB error.
    """
    if _KNOWN_TENANTS and tenant.lower() not in _KNOWN_TENANTS:
        raise HTTPException(status_code=404, detail=f"Unknown tenant: {tenant!r}")


def _get_client_ids(tenant: str) -> List[str]:
    """
    Return list of client UUIDs scoped to tenant.
    migratehq is the admin tenant and sees all clients.
    All other tenants are scoped strictly by the tenant_id FK on clients.
    No LIKE fallback — that was a cross-tenant data leak.
    """
    tenant_lower = tenant.lower()
    if tenant_lower == "migratehq":
        resp = sb.table("clients").select("id").execute()
        return [r["id"] for r in resp.data]

    tenant_resp = sb.table("tenants").select("id").eq("name", tenant_lower).execute()
    if not tenant_resp.data:
        return []

    tenant_id = tenant_resp.data[0]["id"]
    clients_resp = sb.table("clients").select("id").eq("tenant_id", tenant_id).execute()
    return [r["id"] for r in clients_resp.data]


def _latest_runs(client_ids: List[str]) -> List[dict]:
    """Return the latest mapping_run row per client_id."""
    if not client_ids:
        return []
    all_runs = (
        sb.table("mapping_runs")
        .select("*")
        .in_("client_id", client_ids)
        .order("created_at", desc=True)
        .execute()
        .data
    )
    seen: dict[str, dict] = {}
    for run in all_runs:
        cid = run["client_id"]
        if cid not in seen:
            seen[cid] = run
    return list(seen.values())


def _resolve_run_id(client_id: str, run_id: Optional[str]) -> str:
    """
    Return the run_id to use for approve/reject operations.
    If run_id is provided, verify it belongs to client_id and return it.
    If run_id is None, return the latest run_id for the client.
    Raises 404 if the run doesn't exist or doesn't belong to the client.
    """
    if run_id:
        run_check = (
            sb.table("mapping_runs")
            .select("run_id")
            .eq("run_id", run_id)
            .eq("client_id", client_id)
            .execute()
        )
        if not run_check.data:
            raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found for this client")
        return run_id

    run_resp = (
        sb.table("mapping_runs")
        .select("run_id")
        .eq("client_id", client_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not run_resp.data:
        raise HTTPException(status_code=404, detail="No runs found for this client")
    return run_resp.data[0]["run_id"]


# ── endpoints ─────────────────────────────────────────────────────────────────

@app.get("/api/overview")
def get_overview(x_tenant: Optional[str] = Header(None)):
    tenant = x_tenant or "migratehq"
    _validate_tenant(tenant)
    try:
        if USE_BIGQUERY:
            return bigquery_store.get_overview(tenant)

        client_ids = _get_client_ids(tenant)
        if not client_ids:
            return {"total_clients": 0, "overall_accuracy_pct": 0.0,
                    "total_columns_mapped": 0, "flagged_count": 0}

        latest = _latest_runs(client_ids)
        total_clients = len(latest)
        total_cols = sum(r.get("total_columns") or 0 for r in latest)
        total_matched = sum((r.get("l1_count") or 0) + (r.get("l2_count") or 0) for r in latest)
        accuracy = round(total_matched / total_cols * 100, 1) if total_cols else 0.0

        run_ids = [r["run_id"] for r in latest]
        flagged = 0
        if run_ids:
            # Count flagged_for_review across all latest runs
            flagged_resp = (
                sb.table("mapping_results")
                .select("id", count="exact")
                .in_("run_id", run_ids)
                .eq("flagged_for_review", True)
                .execute()
            )
            flagged = flagged_resp.count or 0

        return {
            "total_clients":        total_clients,
            "overall_accuracy_pct": accuracy,
            "total_columns_mapped": total_cols,
            "flagged_count":        flagged,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/clients")
def get_clients(x_tenant: Optional[str] = Header(None)):
    """One summary row per client using their latest run."""
    tenant = x_tenant or "migratehq"
    _validate_tenant(tenant)
    try:
        if USE_BIGQUERY:
            return bigquery_store.get_clients(tenant)

        client_ids = _get_client_ids(tenant)
        if not client_ids:
            return []

        # All clients with their tenant info
        clients_resp = (
            sb.table("clients")
            .select("id, source_dataset, display_name, created_at, tenant_id")
            .in_("id", client_ids)
            .execute()
        )
        clients_by_id = {c["id"]: c for c in clients_resp.data}

        # All runs for these clients
        all_runs = (
            sb.table("mapping_runs")
            .select("*")
            .in_("client_id", client_ids)
            .order("created_at", desc=True)
            .execute()
            .data
        )

        # Count runs per client + find latest
        runs_by_client: dict[str, list] = {}
        for run in all_runs:
            cid = run["client_id"]
            runs_by_client.setdefault(cid, []).append(run)

        results = []
        for cid, runs in runs_by_client.items():
            latest = runs[0]  # already ordered desc
            client = clients_by_id.get(cid, {})

            # Flagged count for latest run
            flagged_resp = (
                sb.table("mapping_results")
                .select("id", count="exact")
                .eq("run_id", latest["run_id"])
                .eq("flagged_for_review", True)
                .execute()
            )
            flagged_count = flagged_resp.count or 0

            total = latest.get("total_columns") or 1
            l1 = latest.get("l1_count") or 0
            l2 = latest.get("l2_count") or 0
            accuracy = latest.get("accuracy_pct") or round((l1 + l2) / total * 100, 1)

            results.append({
                "client_name":          client.get("source_dataset", ""),
                "display_name":         client.get("display_name", ""),
                "last_run_at":          latest.get("created_at", ""),
                "total_runs":           len(runs),
                "latest_accuracy_pct":  accuracy,
                "total_columns":        latest.get("total_columns"),
                "l1_count":             l1,
                "l2_count":             l2,
                "fallback_count":       latest.get("fallback_count"),
                "flagged_count":        flagged_count,
            })

        return results
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/clients/{client_name}/mappings")
def get_mappings(client_name: str, x_tenant: Optional[str] = Header(None)):
    """Column mappings from the latest run for a given client."""
    tenant = x_tenant or "migratehq"
    _validate_tenant(tenant)
    try:
        # Tenant-scope check
        if tenant.lower() != "migratehq":
            if tenant.lower() not in client_name.lower():
                raise HTTPException(status_code=403, detail="Access denied for this client")

        if USE_BIGQUERY:
            return bigquery_store.get_mappings(client_name)

        client_resp = (
            sb.table("clients")
            .select("id")
            .eq("source_dataset", client_name)
            .execute()
        )
        if not client_resp.data:
            return []
        client_id = client_resp.data[0]["id"]

        latest_run = (
            sb.table("mapping_runs")
            .select("run_id")
            .eq("client_id", client_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if not latest_run.data:
            return []
        run_id = latest_run.data[0]["run_id"]

        results = (
            sb.table("mapping_results")
            .select("source_column, target_column, confidence, layer, correct, flagged_for_review, status")
            .eq("run_id", run_id)
            .order("source_column")
            .execute()
        )
        return [
            {
                "source_column":      r["source_column"],
                "target_column":      r["target_column"],
                "confidence":         float(r["confidence"]) if r["confidence"] is not None else 0.0,
                "layer":              r["layer"],
                "correct":            r["correct"],
                "flagged_for_review": r["flagged_for_review"],
                "status":             r.get("status", "pending_review"),
            }
            for r in results.data
        ]
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/clients/{client_name}/runs")
def get_runs(client_name: str, x_tenant: Optional[str] = Header(None)):
    """Full run history for a given client."""
    tenant = x_tenant or "migratehq"
    _validate_tenant(tenant)
    try:
        if tenant.lower() != "migratehq":
            if tenant.lower() not in client_name.lower():
                raise HTTPException(status_code=403, detail="Access denied for this client")

        if USE_BIGQUERY:
            return bigquery_store.get_runs(client_name)

        client_resp = (
            sb.table("clients")
            .select("id")
            .eq("source_dataset", client_name)
            .execute()
        )
        if not client_resp.data:
            return []
        client_id = client_resp.data[0]["id"]

        runs = (
            sb.table("mapping_runs")
            .select("run_id, created_at, total_columns, l1_count, l2_count, fallback_count, accuracy_pct, status")
            .eq("client_id", client_id)
            .order("created_at", desc=True)
            .execute()
        )
        return [
            {
                "run_id":        r["run_id"],
                "created_at":    r["created_at"],
                "total_columns": r["total_columns"],
                "l1_count":      r["l1_count"],
                "l2_count":      r["l2_count"],
                "fallback_count": r["fallback_count"],
                "accuracy_pct":  r["accuracy_pct"],
                "status":        r["status"],
            }
            for r in runs.data
        ]
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/activity")
def get_activity(x_tenant: Optional[str] = Header(None)):
    """Last 10 mapping runs scoped by tenant."""
    tenant = x_tenant or "migratehq"
    _validate_tenant(tenant)
    try:
        if USE_BIGQUERY:
            return bigquery_store.get_activity(tenant)

        client_ids = _get_client_ids(tenant)
        if not client_ids:
            return []

        # Get source_dataset for each client to return it
        clients_resp = (
            sb.table("clients")
            .select("id, source_dataset")
            .in_("id", client_ids)
            .execute()
        )
        source_by_id = {c["id"]: c["source_dataset"] for c in clients_resp.data}

        runs = (
            sb.table("mapping_runs")
            .select("run_id, client_id, created_at, total_columns, l1_count, l2_count, fallback_count, accuracy_pct")
            .in_("client_id", client_ids)
            .order("created_at", desc=True)
            .limit(10)
            .execute()
        )
        return [
            {
                "run_id":         r["run_id"],
                "source_dataset": source_by_id.get(r["client_id"], ""),
                "last_run_at":    r["created_at"],
                "total_columns":  r["total_columns"],
                "l1_count":       r["l1_count"],
                "l2_count":       r["l2_count"],
                "accuracy_pct":   r["accuracy_pct"],
            }
            for r in runs.data
        ]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/clients/{client_name}/mappings/{source_column}/explain")
def explain_mapping(
    client_name: str,
    source_column: str,
    x_tenant: Optional[str] = Header(None),
):
    """
    Return a natural-language explanation for a flagged mapping.
    Generates via Gemini on first call; caches in mapping_results.explanation.
    """
    tenant = x_tenant or "migratehq"
    _validate_tenant(tenant)
    try:
        if tenant.lower() != "migratehq":
            if tenant.lower() not in client_name.lower():
                raise HTTPException(status_code=403, detail="Access denied for this client")

        if USE_BIGQUERY:
            row = bigquery_store.get_mapping_row(client_name, source_column)
            if not row:
                raise HTTPException(status_code=404, detail="Mapping row not found")
            explanation = _generate_explanation(row)
            return {"explanation": explanation, "cached": False}

        client_resp = sb.table("clients").select("id").eq("source_dataset", client_name).execute()
        if not client_resp.data:
            raise HTTPException(status_code=404, detail="Client not found")
        client_id = client_resp.data[0]["id"]

        run_resp = (
            sb.table("mapping_runs")
            .select("run_id")
            .eq("client_id", client_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if not run_resp.data:
            raise HTTPException(status_code=404, detail="No runs found")
        run_id = run_resp.data[0]["run_id"]

        # Select all columns so this works even if explanation/sample columns
        # haven't been migrated yet — new fields will simply be absent from the row.
        row_resp = (
            sb.table("mapping_results")
            .select("*")
            .eq("run_id", run_id)
            .eq("source_column", source_column)
            .execute()
        )
        if not row_resp.data:
            raise HTTPException(status_code=404, detail="Mapping row not found")
        row = row_resp.data[0]

        if row.get("explanation"):
            return {"explanation": row["explanation"], "cached": True}

        explanation = _generate_explanation(row)

        if explanation:
            try:
                sb.table("mapping_results").update({"explanation": explanation}).eq("id", row["id"]).execute()
            except Exception:
                pass  # non-fatal — still return the explanation

        return {"explanation": explanation, "cached": False}

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/clients/{client_name}/mappings/{source_column}/approve")
def approve_mapping(
    client_name: str,
    source_column: str,
    x_tenant: Optional[str] = Header(None),
    run_id: Optional[str] = Query(None, description="Target a specific run; defaults to latest"),
):
    """
    Mark a mapping as approved. If the record was pending_review, also writes it to BigQuery.
    High-confidence records are auto-approved at pipeline write time and already in BigQuery,
    so the BQ write here only fires for records that were previously pending_review.
    Pass ?run_id=<uuid> to target an older run; omit to default to the client's latest run.
    """
    tenant = x_tenant or "migratehq"
    _validate_tenant(tenant)
    if USE_BIGQUERY:
        raise HTTPException(status_code=503, detail="Review workflow requires Supabase; currently running in BigQuery-only mode")
    try:
        if tenant.lower() != "migratehq":
            if tenant.lower() not in client_name.lower():
                raise HTTPException(status_code=403, detail="Access denied for this client")

        client_resp = sb.table("clients").select("id").eq("source_dataset", client_name).execute()
        if not client_resp.data:
            raise HTTPException(status_code=404, detail="Client not found")
        client_id = client_resp.data[0]["id"]

        run_id = _resolve_run_id(client_id, run_id)

        row_resp = (
            sb.table("mapping_results")
            .select("*")
            .eq("run_id", run_id)
            .eq("source_column", source_column)
            .execute()
        )
        if not row_resp.data:
            raise HTTPException(status_code=404, detail="Mapping row not found")
        row = row_resp.data[0]

        was_pending = row.get("status") == "pending_review"
        sb.table("mapping_results").update({"status": "approved"}).eq("id", row["id"]).execute()

        # Push to BigQuery only if this record was pending_review — high-confidence records
        # were already written to BQ at pipeline time, so we skip to avoid duplicates.
        bq_written = False
        if was_pending:
            try:
                bigquery_store.push_approved_result(row)
                bq_written = True
            except Exception as bq_exc:
                logger.warning(
                    "BigQuery write failed for approved result %s/%s: %s",
                    client_name, source_column, bq_exc,
                )

        return {"source_column": source_column, "status": "approved", "bq_written": bq_written}

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/clients/{client_name}/mappings/{source_column}/reject")
def reject_mapping(
    client_name: str,
    source_column: str,
    x_tenant: Optional[str] = Header(None),
    run_id: Optional[str] = Query(None, description="Target a specific run; defaults to latest"),
):
    """
    Mark a mapping as rejected. Rejected records are never written to BigQuery.
    Pass ?run_id=<uuid> to target an older run; omit to default to the client's latest run.
    """
    tenant = x_tenant or "migratehq"
    _validate_tenant(tenant)
    if USE_BIGQUERY:
        raise HTTPException(status_code=503, detail="Review workflow requires Supabase; currently running in BigQuery-only mode")
    try:
        if tenant.lower() != "migratehq":
            if tenant.lower() not in client_name.lower():
                raise HTTPException(status_code=403, detail="Access denied for this client")

        client_resp = sb.table("clients").select("id").eq("source_dataset", client_name).execute()
        if not client_resp.data:
            raise HTTPException(status_code=404, detail="Client not found")
        client_id = client_resp.data[0]["id"]

        run_id = _resolve_run_id(client_id, run_id)

        row_resp = (
            sb.table("mapping_results")
            .select("id")
            .eq("run_id", run_id)
            .eq("source_column", source_column)
            .execute()
        )
        if not row_resp.data:
            raise HTTPException(status_code=404, detail="Mapping row not found")

        sb.table("mapping_results").update({"status": "rejected"}).eq("id", row_resp.data[0]["id"]).execute()
        return {"source_column": source_column, "status": "rejected"}

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def _generate_explanation(row: dict) -> Optional[str]:
    """Call Gemini to produce a human-readable mapping explanation. Returns None on failure."""
    gemini_key = os.environ.get("GOOGLE_API_KEY")
    if not gemini_key:
        return None

    src_samples = row.get("source_samples") or []
    tgt_samples = row.get("target_samples") or []
    target_col  = row.get("target_column") or "no match"
    layer       = row.get("layer", "")
    confidence  = float(row.get("confidence") or 0.0)

    layer_desc = {
        "L1":         "lexical/string similarity matching",
        "L2":         "AI semantic reasoning",
        "L1-fallback":"low-confidence fallback from string matching",
        "none":       "no match found",
    }.get(layer, "automated matching")

    samples_block = ""
    if src_samples:
        samples_block += f"\nSource column sample values: {src_samples[:5]}"
    if tgt_samples:
        samples_block += f"\nTarget column sample values: {tgt_samples[:5]}"

    prompt = f"""You are explaining a database column mapping to a non-technical operations reviewer.

Source column: '{row["source_column"]}'
Predicted target column: '{target_col}'
Detection method: {layer_desc}
Confidence: {confidence:.0%}{samples_block}

Write 1-2 sentences in plain English:
- State WHY these two columns likely match (or why the match may be wrong)
- Reference column names and sample values where available to make it concrete
- Flag any concern if the match seems questionable at this confidence level

Return ONLY the explanation — no JSON, no bullet points, no preamble."""

    try:
        from google import genai as google_genai
        gemini = google_genai.Client(api_key=gemini_key)

        # Single attempt — don't retry inside an interactive HTTP request.
        # 429s have long retry-after windows (30s+) that would time out the UI.
        response = gemini.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        return response.text.strip()
    except Exception:
        return None


@app.post("/api/ingest")
def ingest(
    source_file: UploadFile = File(...),
    target_file: UploadFile = File(...),
    client_name: str = Form(...),
    x_tenant: Optional[str] = Header(None),
):
    """
    Upload source and target CSVs, run the L1→L2 mapping pipeline, persist results.

    Synchronous — the request blocks until the pipeline completes (L1 + L2 + BQ write).
    Typical runtime: 30s–several minutes depending on column count and L2 escalations.

    Form fields:
        source_file  — the customer's raw data CSV
        target_file  — the WMS target schema CSV to map against
        client_name  — label stored as source_dataset in BigQuery and Supabase
    """
    tenant = x_tenant or "migratehq"
    _validate_tenant(tenant)

    # ── Validate files ────────────────────────────────────────────────────────
    for upload, label in [(source_file, "source_file"), (target_file, "target_file")]:
        if not upload.filename:
            raise HTTPException(status_code=400, detail=f"{label}: no filename provided")
        if not upload.filename.lower().endswith(".csv"):
            raise HTTPException(status_code=400, detail=f"{label}: only CSV files are accepted")

    source_bytes = source_file.file.read()
    target_bytes = target_file.file.read()

    if len(source_bytes) == 0:
        raise HTTPException(status_code=400, detail="source_file is empty")
    if len(target_bytes) == 0:
        raise HTTPException(status_code=400, detail="target_file is empty")
    if len(source_bytes) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"source_file exceeds 500 MB limit")
    if len(target_bytes) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"target_file exceeds 500 MB limit")

    # ── Write to temp files ────────────────────────────────────────────────────
    # NamedTemporaryFile with delete=False so the path remains valid after close.
    # Both files are cleaned up in the finally block regardless of outcome.
    src_tmp = None
    tgt_tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            f.write(source_bytes)
            src_tmp = f.name
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            f.write(target_bytes)
            tgt_tmp = f.name

        # ── Run pipeline ───────────────────────────────────────────────────────
        result = run_mapping_pipeline(
            source_csv_path=src_tmp,
            target_csv_path=tgt_tmp,
            dataset_name=client_name,
            client_label=client_name,
            tenant_name=tenant,
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Ingest pipeline error for client=%s: %s", client_name, exc)
        raise HTTPException(status_code=500, detail=f"Pipeline error: {exc}")
    finally:
        for path in (src_tmp, tgt_tmp):
            if path and os.path.exists(path):
                os.unlink(path)

    # ── Handle pipeline-level errors ──────────────────────────────────────────
    if result.get("status") == "error":
        raise HTTPException(status_code=422, detail=result.get("error", "Pipeline failed"))

    total = result.get("total_columns") or 1
    l1    = result.get("l1_count") or 0
    l2    = result.get("l2_count") or 0

    return {
        "run_id":         result["run_id"],
        "created_at":     result.get("timestamp"),
        "total_columns":  result.get("total_columns"),
        "l1_count":       l1,
        "l2_count":       l2,
        "fallback_count": result.get("fallback_count"),
        "accuracy_pct":   round((l1 + l2) / total * 100, 1),
        "status":         "completed",
    }


@app.get("/health")
def health():
    return {"status": "ok", "data_source": "bigquery" if USE_BIGQUERY else "supabase"}
