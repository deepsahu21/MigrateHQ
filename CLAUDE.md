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

**Review queue + confidence gate — resolved.** `mapping_results` now has a `status` column (`pending_review` / `approved` / `rejected`). High-confidence records (≥ 0.75) are auto-approved at pipeline write time and written to BigQuery immediately. Low-confidence records go to Supabase only as `pending_review`. `POST /approve` and `POST /reject` endpoints exist; /approve triggers the BigQuery write for the newly approved row. Approve/Override buttons in `Clients.tsx` are wired with loading state and optimistic queue removal. See `build_sesh_prompt_outputs/prompt2.txt`. **Known gap:** existing Supabase rows need a one-time backfill (`UPDATE mapping_results SET status='approved' WHERE confidence >= 0.75`) — the ALTER TABLE default sets them all to `pending_review`.

The `correct` field in `mapping_results` is always `null` (the pipeline writes `None`); the "Correct" column in the UI always shows "—".

**WMS export does not exist.**
No file-export endpoint, no WMS template generation, no output formatter anywhere in the codebase. The file-export-not-API-writeback decision is a standing architectural intent, not implemented code.

**POST /api/ingest (built, prompt3 + addendum).** Accepts `source_file` + `target_file` (CSV uploads, 500 MB limit each) + `client_name` form field + `X-Tenant` header. Saves to OS temp files, calls `run_mapping_pipeline()` synchronously, cleans up, returns a run summary matching the shape of the runs history endpoint. Validation: CSV-only, non-empty, size limit. `python-multipart`, `pandas`, `valentine` added to `backend/requirements.txt` and installed. **No frontend upload UI yet.** Key limitations: synchronous (blocks connection for full pipeline duration); no auth beyond tenant whitelist validation.

**Schema.sql — resolved.** `source_samples JSONB`, `target_samples JSONB`, and `explanation TEXT` added to the `mapping_results` CREATE TABLE definition. Idempotent `ALTER TABLE ADD COLUMN IF NOT EXISTS` statements also added for existing deployments. See `build_sesh_prompt_outputs/prompt1.txt`.

**BigQuery and Supabase schemas diverge for the same logical tables:**
- `mapping_runs` in BQ has `run_timestamp`, `source_dataset`, `target_dataset`, `notes` — Supabase has `created_at`, `accuracy_pct`, `status`, `client_id` (FK) instead.
- `mapping_results` in BQ has `run_timestamp` but no `flagged_for_review`. Supabase has `flagged_for_review` but no `run_timestamp`.
- `bigquery_store.py` flagging uses `WHERE r.confidence < 0.75` (computed on-the-fly), not the `flagged_for_review` column.

**BigQuery fallback path gaps.**
`bigquery_store.get_mapping_row()` returns `r.*` but BigQuery results table has no `explanation`, `source_samples`, or `target_samples` columns, so the `/explain` endpoint always generates fresh (never cached) when running in BigQuery mode.

**No backend authentication exists (partially mitigated, prompt5).**
There is no JWT validation, no server-side session check, and no middleware on any endpoint in `backend/main.py`. Frontend auth is two hardcoded demo accounts checked entirely client-side in `frontend/src/lib/auth.ts`; passing login does not create any server-side session. Full JWT/OAuth auth is intentionally deferred.

**Tenant whitelist validation — resolved (prompt5).** At startup, `_KNOWN_TENANTS` is loaded from the `tenants` Supabase table (or hardcoded to `{"migratehq", "olist"}` in BigQuery-only mode). Every endpoint now calls `_validate_tenant(tenant)` immediately after reading the `X-Tenant` header; unknown tenant names receive HTTP 404. Validation is fail-open if the startup load fails. **Remaining gap:** the backend validates that a tenant EXISTS but does not verify the caller is PERMITTED to access it — a user who knows a valid tenant name can still spoof the header and read that tenant's data. Aggregate endpoints like `/overview` also do not fully scope to a single tenant. See `build_sesh_prompt_outputs/prompt5.txt` for cross-check questions on these remaining gaps.

**L2 batching — resolved (prompt4).** `orchestrator.py` now calls `semantic_match_batch()` directly (not `reason_best_match()`). Escalated columns are grouped by `_group_by_category()` into `timestamps / identifiers / status / other` using token-based name matching. One Gemini call is made per non-empty group with full mutual-exclusion reasoning across all columns in the group. For the Olist stage2 dataset: 3 calls instead of 8. Sleep is kept between batch calls. `reason_best_match()` still exists in `reasoner.py` for backward compat but is no longer called by orchestrator. **Open question**: the auto-grouping puts all 5 Olist date columns in one batch vs test_stage2's hand-tuned 4-batch split — accuracy vs the ground truth has not been re-validated with a live run.

**Tenant hardcoded to 'olist' in write path — resolved.** `write_to_supabase()` now accepts `tenant_name: str = "olist"`. `run_mapping_pipeline()` accepts the same parameter and threads it through. Default preserves backward compatibility. See `build_sesh_prompt_outputs/prompt1.txt`.

---

## 4. KEY ARCHITECTURAL PRINCIPLES

**Confidence gate (enforced as of prompt2):** Records with confidence ≥ 0.75 are auto-approved and written to BigQuery at pipeline time. Records below 0.75 are held in Supabase as `pending_review` and reach BigQuery only after a human approves them via `POST /approve`. Rejected records never reach BigQuery. The gate lives in `bigquery_loader.py:push_mapping_to_bigquery()` (filter) and `bigquery_store.py:push_approved_result()` (single-row write on approval).

**File export, not API write-back:** The output of an approved mapping run should be a CSV/file in the WMS's native import template format. Do not build live API connectors to WMS systems. This keeps the integration surface simple and auditable.

**BigQuery is the source of truth for pipeline output; Supabase is the operational layer.** BigQuery gets written first (blocking) — if it fails, the pipeline returns an error. Supabase gets mirrored second (non-fatal). The backend API prefers Supabase for reads (lower latency, richer query options) and falls back to BigQuery if Supabase is unreachable at startup.

**L1 threshold (0.8) ≠ flagging threshold (0.75).** L1 accepts at ≥0.8. Items are flagged for review at <0.75. The gap (0.75–0.80) means some L2-resolved columns won't be flagged even though they weren't strong L1 matches. This is intentional — L2 can have high confidence on semantically clear matches.

**1-second sleep between L2 LLM calls** in `orchestrator.py:98` to avoid rate-limiting. Do not remove without also adding retry/backoff logic.

**WMS domain context** lives in `schema-mapping/claude.md` (not this file) and is loaded by `reasoner.py` at import time. It contains few-shot examples for WMS column name mappings. Edit that file to tune L2 reasoning; don't put it here.

---

## 6. BUILD SESSION LOG CONVENTION

After every build task completed in any session, a verification file is written to `build_sesh_prompt_outputs/promptN.txt` (N increments; check the folder for the next available number before writing). Each file records: the task, every file changed with diffs or new signatures, what was actually built, how it was tested, assumptions made, known limitations, and cross-check questions for independent review.

Before assuming project state from this CLAUDE.md alone, read `build_sesh_prompt_outputs/` — it is the authoritative log of what has actually been built, tested, and deferred. CLAUDE.md is updated periodically but the prompt files have finer-grained detail per task.

Future sessions must continue this convention. Check the folder at the start of any build session to establish current state.

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
| **DB schema** | `backend/schema.sql` — includes `source_samples`, `target_samples`, `explanation`, `status` columns (added prompt1/prompt2) |
| **Frontend pages** | `frontend/src/pages/` — Login.tsx, Overview.tsx, Clients.tsx, Analytics.tsx, Settings.tsx |
| **API client** | `frontend/src/lib/api.ts` |
| **Auth / session** | `frontend/src/lib/auth.ts` |
| **Dev launcher** | `start.sh` — kills :8000/:5173, starts uvicorn + vite |
| **Env vars (backend)** | `backend/.env` — SUPABASE_URL, SUPABASE_SECRET_KEY, SUPABASE_DB_*, GOOGLE_API_KEY |
| **Env vars (pipeline)** | `schema-mapping/.env` — GOOGLE_API_KEY, SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, SUPABASE_SECRET_KEY, SUPABASE_DB_* |
| **Pipeline tests** | `schema-mapping/test_stage2.py`, `test_stage3.py`, `test_pipeline_e2e.py` |
| **Accuracy reporting** | `schema-mapping/accuracy_report.py` |
