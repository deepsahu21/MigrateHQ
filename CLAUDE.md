# MigrateHQ — Session Context

## 1. PROJECT SUMMARY

MigrateHQ is a schema mapping engine that automatically maps source database columns to WMS (warehouse/order management) target columns using a two-layer pipeline: lexical matching (Layer 1, Valentine library) followed by LLM semantic reasoning (Layer 2, Gemini). The current pilot target is the Olist e-commerce dataset being mapped to manufactured WMS schemas. The standing architectural decision on WMS integration is **file export via a native WMS import template, not live API write-back** — no WMS connector code exists yet anywhere in the codebase.

---

## 2. BUILT AND WORKING

**Schema mapping pipeline**
- `schema-mapping/orchestrator.py` — L1→L2 pipeline. L1 uses `JaccardDistanceMatcher` from Valentine at threshold 0.8. Columns below threshold escalate to L2; L2 failures fall back to best unclaimed L1 candidate.
- `schema-mapping/src/layer2_claude/reasoner.py` — Gemini `gemini-2.5-flash` via `google-genai` SDK. Loads WMS few-shot context from `schema-mapping/claude.md` at import time. `reason_best_match()` is the legacy single-column entry point called by orchestrator; `semantic_match_batch()` is the real implementation underneath.
- `schema-mapping/orchestrator_pipeline.py` — `run_mapping_pipeline()`: loads two CSVs → `run_orchestrator()` → `push_mapping_to_bigquery()` (blocking) → `write_to_supabase()` (non-fatal).

**Storage — BigQuery (primary write target)**
- `schema-mapping/bigquery_loader.py` — `push_mapping_to_bigquery()` writes `mapping_runs` + `mapping_results` rows. Also contains `write_to_supabase()` which mirrors run data to Supabase.
- Project: `project-bf89f8dc-434b-4108-be6`, dataset: `migratehq`.

**Storage — Supabase (operational / API read layer)**
- `backend/schema.sql` defines: `tenants`, `clients`, `mapping_runs`, `mapping_results`.
- `backend/bigquery_store.py` — full BigQuery read fallback (activated at startup if Supabase is unreachable, controlled by `USE_BIGQUERY` flag set once on startup).

**Backend API** (`backend/main.py`, FastAPI, port 8000)
- `GET /health` — liveness; reports `data_source: "supabase" | "bigquery"`.
- `GET /api/overview` — aggregate stats (total_clients, accuracy, flagged_count) scoped by `X-Tenant` header.
- `GET /api/clients` — one row per client from their latest run; includes flagged_count per run.
- `GET /api/clients/{client_name}/mappings` — all column mappings from the client's latest run.
- `GET /api/clients/{client_name}/runs` — full run history for a client.
- `GET /api/activity` — last 10 runs across all clients, scoped by tenant.
- `POST /api/clients/{client_name}/mappings/{source_column}/explain` — generates a Gemini explanation for a flagged mapping; caches result in `mapping_results.explanation`. Uses `gemini-2.5-flash` in `_generate_explanation()`.

**Frontend** (`frontend/`, React + TypeScript + Vite, port 5173)
- 5 pages: `Login.tsx` (client-side auth), `Overview.tsx` (overview stats + activity feed), `Clients.tsx` (list + detail + tabs), `Analytics.tsx`, `Settings.tsx`.
- `Clients.tsx` — client list, stat cards, Column Mappings tab, Run History tab, Review Queue tab, inline NL explanation on row expand (calls `/explain` endpoint, caches client-side).
- Auth: session + tenant scoping; admin role shows "All Clients", others see "Your Data".
- `start.sh` — launches backend + frontend together; kills existing processes on :8000/:5173 first.

**Dev environment**
- Python venv: `.venv-backend/` at repo root (shared across backend + schema-mapping).
- `start.sh` expects `.venv-backend/bin/uvicorn` to exist.
- No Docker, no CI, no cloud deploy config (no `railway.toml`, `Dockerfile`, `.github/`). Local-only.

---

## 3. NOT BUILT / KNOWN GAPS

**Review Queue Approve/Override buttons are visual only.**
`Clients.tsx:354-357` renders `<button className="btn btn-approve">Approve</button>` and `<button className="btn btn-override">Override</button>` with no `onClick` handlers. No backend endpoints exist for approval or override actions. The `correct` field in `mapping_results` is always `null` (the pipeline writes `None`); the "Correct" column in the UI always shows "—".

**Confidence gate is NOT enforced.**
The pipeline writes 100% of mapping results to BigQuery and Supabase unconditionally — low-confidence results are stored alongside high-confidence ones. Flagging (`flagged_for_review = confidence < 0.75`) is metadata only; no approval gate prevents flagged mappings from persisting. The review-then-approve flow is UI-incomplete (see above).

**WMS export does not exist.**
No file-export endpoint, no WMS template generation, no output formatter anywhere in the codebase. The file-export-not-API-writeback decision is a standing architectural intent, not implemented code.

**Schema.sql is out of date — three columns missing.**
`write_to_supabase()` in `bigquery_loader.py` inserts `source_samples`, `target_samples` (both written at pipeline time), and the explain endpoint updates `explanation` (written on first `/explain` call). None of these columns appear in `backend/schema.sql`. They exist in the live Supabase DB but are not in the repo's schema definition.

**BigQuery and Supabase schemas diverge for the same logical tables:**
- `mapping_runs` in BQ has `run_timestamp`, `source_dataset`, `target_dataset`, `notes` — Supabase has `created_at`, `accuracy_pct`, `status`, `client_id` (FK) instead.
- `mapping_results` in BQ has `run_timestamp` but no `flagged_for_review`. Supabase has `flagged_for_review` but no `run_timestamp`.
- `bigquery_store.py` flagging uses `WHERE r.confidence < 0.75` (computed on-the-fly), not the `flagged_for_review` column.

**BigQuery fallback path gaps.**
`bigquery_store.get_mapping_row()` returns `r.*` but BigQuery results table has no `explanation`, `source_samples`, or `target_samples` columns, so the `/explain` endpoint always generates fresh (never cached) when running in BigQuery mode.

**No backend authentication exists.**
There is no JWT validation, no server-side session check, and no middleware on any endpoint in `backend/main.py`. The `X-Tenant` header is read and used to scope queries, but it is never validated against any authenticated identity — any client can send any `X-Tenant` value and read that tenant's data. Frontend auth is two hardcoded demo accounts checked entirely client-side in `frontend/src/lib/auth.ts` (or `Login.tsx`); passing login does not create any server-side session. This is a known gap, intentionally deferred.

**L2 batching is not wired into the production pipeline.**
`reasoner.py` contains `semantic_match_batch()` which can match multiple source columns against multiple targets in a single LLM call with mutual-exclusion reasoning. However, `orchestrator.py` calls `reason_best_match()` per column, which wraps `semantic_match_batch()` with a batch size of 1. Production L2 is therefore still functionally sequential single-column processing — one Gemini call per escalated column, with the 1-second sleep between calls. True batched matching is exercised in `schema-mapping/test_stage2.py` but is not wired into `orchestrator.py`.

**Tenant hardcoded to 'olist' in write path.**
`write_to_supabase()` hardcodes `tenant='olist'` — if a different tenant's data is pushed through the pipeline, the Supabase write will fail silently (non-fatal log warning).

---

## 4. KEY ARCHITECTURAL PRINCIPLES

**Confidence gate (intended, not yet enforced):** Only human-approved mappings should ultimately reach the WMS export file. The review queue exists to surface low-confidence mappings (<0.75) for human sign-off before export. Currently, everything is written to storage regardless of confidence and review status. The Approve button must be wired before any WMS export feature is built.

**File export, not API write-back:** The output of an approved mapping run should be a CSV/file in the WMS's native import template format. Do not build live API connectors to WMS systems. This keeps the integration surface simple and auditable.

**BigQuery is the source of truth for pipeline output; Supabase is the operational layer.** BigQuery gets written first (blocking) — if it fails, the pipeline returns an error. Supabase gets mirrored second (non-fatal). The backend API prefers Supabase for reads (lower latency, richer query options) and falls back to BigQuery if Supabase is unreachable at startup.

**L1 threshold (0.8) ≠ flagging threshold (0.75).** L1 accepts at ≥0.8. Items are flagged for review at <0.75. The gap (0.75–0.80) means some L2-resolved columns won't be flagged even though they weren't strong L1 matches. This is intentional — L2 can have high confidence on semantically clear matches.

**1-second sleep between L2 LLM calls** in `orchestrator.py:98` to avoid rate-limiting. Do not remove without also adding retry/backoff logic.

**WMS domain context** lives in `schema-mapping/claude.md` (not this file) and is loaded by `reasoner.py` at import time. It contains few-shot examples for WMS column name mappings. Edit that file to tune L2 reasoning; don't put it here.

---

## 5. FILE MAP

| Feature area | Files |
|---|---|
| **Pipeline entry point** | `schema-mapping/orchestrator_pipeline.py` → `run_mapping_pipeline()` |
| **L1 matching** | `schema-mapping/orchestrator.py` → `run_orchestrator()`, uses `valentine.algorithms.JaccardDistanceMatcher` |
| **L2 reasoning** | `schema-mapping/src/layer2_claude/reasoner.py` → `semantic_match_batch()` / `reason_best_match()` |
| **L2 domain context** | `schema-mapping/claude.md` (WMS few-shot examples, loaded at import time) |
| **BigQuery write** | `schema-mapping/bigquery_loader.py` → `push_mapping_to_bigquery()`, `write_to_supabase()` |
| **BigQuery read (fallback)** | `backend/bigquery_store.py` — all read functions mirror `main.py` API surface |
| **API server** | `backend/main.py` — all endpoints, Supabase client, Gemini explain |
| **DB schema** | `backend/schema.sql` (WARNING: missing explanation/source_samples/target_samples columns) |
| **Frontend pages** | `frontend/src/pages/` — Login.tsx, Overview.tsx, Clients.tsx, Analytics.tsx, Settings.tsx |
| **API client** | `frontend/src/lib/api.ts` |
| **Auth / session** | `frontend/src/lib/auth.ts` |
| **Dev launcher** | `start.sh` — kills :8000/:5173, starts uvicorn + vite |
| **Env vars (backend)** | `backend/.env` — SUPABASE_URL, SUPABASE_SECRET_KEY, SUPABASE_DB_*, GOOGLE_API_KEY |
| **Env vars (pipeline)** | `schema-mapping/.env` — GOOGLE_API_KEY, SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, SUPABASE_SECRET_KEY, SUPABASE_DB_* |
| **Pipeline tests** | `schema-mapping/test_stage2.py`, `test_stage3.py`, `test_pipeline_e2e.py` |
| **Accuracy reporting** | `schema-mapping/accuracy_report.py` |
