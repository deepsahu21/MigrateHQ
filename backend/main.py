"""
MigrateHQ FastAPI backend — Supabase data layer.

Run with: uvicorn main:app --reload --port 8000
Auth: SUPABASE_URL + SUPABASE_SECRET_KEY in backend/.env
"""
import os
import re
import time
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

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


# ── helpers ───────────────────────────────────────────────────────────────────

def _get_client_ids(tenant: str) -> Optional[List[str]]:
    """
    Return list of client UUIDs scoped to tenant, or None for admin (all clients).
    """
    tenant_lower = tenant.lower()
    if tenant_lower == "migratehq":
        resp = sb.table("clients").select("id").execute()
        return [r["id"] for r in resp.data]

    tenant_resp = sb.table("tenants").select("id").eq("name", tenant_lower).execute()
    if not tenant_resp.data:
        # Fall back to LIKE match on source_dataset
        clients_resp = sb.table("clients").select("id").ilike("source_dataset", f"%{tenant_lower}%").execute()
        return [r["id"] for r in clients_resp.data]

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


# ── endpoints ─────────────────────────────────────────────────────────────────

@app.get("/api/overview")
def get_overview(x_tenant: Optional[str] = Header(None)):
    tenant = x_tenant or "migratehq"
    try:
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
    try:
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
    try:
        # Tenant-scope check
        if tenant.lower() != "migratehq":
            if tenant.lower() not in client_name.lower():
                raise HTTPException(status_code=403, detail="Access denied for this client")

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
            .select("source_column, target_column, confidence, layer, correct, flagged_for_review")
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
    try:
        if tenant.lower() != "migratehq":
            if tenant.lower() not in client_name.lower():
                raise HTTPException(status_code=403, detail="Access denied for this client")

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
    try:
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
    try:
        if tenant.lower() != "migratehq":
            if tenant.lower() not in client_name.lower():
                raise HTTPException(status_code=403, detail="Access denied for this client")

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


@app.get("/health")
def health():
    return {"status": "ok"}
