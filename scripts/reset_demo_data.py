#!/usr/bin/env python3
"""
Dev/demo-only reset for seeded MigrateHQ data.

Deletes mapping_results + mapping_runs rows for the targeted clients from BOTH
BigQuery and Supabase, along with the revoke/export lifecycle records that hang
off them (exports, export_mappings, mapping_status_history, the client's
mapping_rules cache entries, and the BigQuery mapping_revocations ledger), returning those clients to a clean pre-run state. The
clients rows themselves are kept, so re-running a pipeline reuses the same
client record (and the same client_id) rather than creating a duplicate.

    python scripts/reset_demo_data.py --dry-run
    python scripts/reset_demo_data.py --tenant tenant_1 --client clientA
    python scripts/reset_demo_data.py --tenant tenant_1,tenant_2 --dry-run
    python scripts/reset_demo_data.py --tenant all --yes

Tenant defaults to 'migratehq'. Note that the seeded demo clients (clientA,
clientB) actually live under tenant_1 / tenant_2 — migratehq has no clients at
all — so a real demo reset needs an explicit --tenant. clientA exists under
BOTH of those tenants, which is why every lookup here is scoped by tenant_id
and never by client name alone.

DELETE, not status-reset: a status flip back to 'pending_review' would leave
the run rows in place, so the UI would still show run history, accuracy and
"latest run" stats. That is not a pre-run state. Deleting the runs makes the
client fall out of /api/clients entirely, which is what a fresh demo needs.

WHY BIGQUERY IS DELETED FIRST
-----------------------------
BigQuery has no tenant_id column and no client column. Its only join key back
to a tenant is run_id, and the run_id -> client -> tenant chain lives ONLY in
Supabase. So the order is forced:

    1. resolve run_ids from Supabase   (read-only)
    2. delete those run_ids from BigQuery
    3. only then delete them from Supabase

If Supabase went first and BigQuery then failed, the run_ids would be gone and
the orphaned BigQuery rows would be unidentifiable — an unrecoverable
half-done state. This order makes the failure recoverable instead: a Supabase
failure leaves the mapping intact, so the command can simply be re-run.

NOT wired into the API or the frontend, by design. This is local tooling.
Never touches schema (no DDL), never touches another tenant's rows, and
refuses outright to operate on a protected tenant.
"""
import argparse
import json
import os
import shutil
import sys
import uuid
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv
from supabase import create_client, Client

# Default blast radius. Overridable with --tenant, because the seeded demo
# clients live under tenant_1 / tenant_2 rather than here. The default stays
# migratehq so the bare no-argument invocation is the most conservative one.
DEFAULT_TENANT = "migratehq"

# Never resettable, not even when named explicitly with --tenant, and never
# reachable via `--tenant all`. This is the one hard guarantee the script
# makes; enforced immediately after arg parsing, before any connection opens.
PROTECTED_TENANTS = {"olist"}

# The only tenants `--tenant all` will expand to. An explicit allowlist rather
# than "every tenant in the tenants table" on purpose: a tenant added later is
# not silently swept into a destructive default. Protected tenants are
# subtracted from this set regardless of what is listed here.
DEMO_TENANTS = {"migratehq", "tenant_1", "tenant_2"}

# Supabase/PostgREST puts the filter list in the URL, so an unbounded .in_()
# eventually exceeds the URL length limit. Same chunking main.py uses.
CHUNK = 100

# Mirrors schema-mapping/bigquery_loader.py. Duplicated rather than imported so
# this script does not drag in the pipeline's import chain (valentine, pandas,
# the Gemini SDK) just to run a delete.
BQ_PROJECT = "project-bf89f8dc-434b-4108-be6"
BQ_DATASET = "migratehq"
BQ_RUNS_TABLE = f"{BQ_PROJECT}.{BQ_DATASET}.mapping_runs"
BQ_RESULTS_TABLE = f"{BQ_PROJECT}.{BQ_DATASET}.mapping_results"
# Append-only revocation ledger (prompt28). Keyed by run_id like the others, so
# it resets on exactly the same scope.
BQ_REVOCATIONS_TABLE = f"{BQ_PROJECT}.{BQ_DATASET}.mapping_revocations"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BigQueryUnavailable(RuntimeError):
    """BigQuery could not be reached or queried. Never partially applied."""


# ── connections ───────────────────────────────────────────────────────────────

def _connect_supabase() -> Client:
    """Reuse the backend's credentials; fall back to the pipeline's .env."""
    for env_path in (
        os.path.join(_REPO_ROOT, "backend", ".env"),
        os.path.join(_REPO_ROOT, "schema-mapping", ".env"),
    ):
        if os.path.exists(env_path):
            load_dotenv(env_path)

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SECRET_KEY")
    if not url or not key:
        sys.exit(
            "error: SUPABASE_URL / SUPABASE_SECRET_KEY not found.\n"
            "       Expected them in backend/.env or schema-mapping/.env."
        )
    return create_client(url, key)


def _connect_bigquery():
    """Imported lazily so --help and arg validation work without BQ creds."""
    try:
        from google.cloud import bigquery
        return bigquery.Client(project=BQ_PROJECT), bigquery
    except Exception as exc:
        raise BigQueryUnavailable(str(exc))


def _chunks(items):
    for i in range(0, len(items), CHUNK):
        yield items[i:i + CHUNK]


# ── resolution (read-only) ────────────────────────────────────────────────────

def _resolve_tenants(sb: Client, raw: str) -> List[Tuple[str, str]]:
    """Parse --tenant into an ordered list of (name, tenant_id).

    Accepts a single name, a comma-separated list, or the literal 'all'.
    Protected tenants are refused explicitly and excluded from 'all'.
    """
    known = {t["name"]: t["id"] for t in (sb.table("tenants").select("id, name").execute().data or [])}

    if raw.strip().lower() == "all":
        names = sorted((DEMO_TENANTS - PROTECTED_TENANTS) & set(known))
        if not names:
            sys.exit(
                "error: --tenant all matched no tenants.\n"
                f"       Demo allowlist: {', '.join(sorted(DEMO_TENANTS)) or '(empty)'}\n"
                f"       Present in DB : {', '.join(sorted(known)) or '(none)'}"
            )
    else:
        names = [n.strip() for n in raw.split(",") if n.strip()]
        if not names:
            sys.exit("error: --tenant was empty.")

    # Dedupe, preserving order, so `--tenant tenant_1,tenant_1` is harmless.
    seen: Set[str] = set()
    ordered = [n for n in names if not (n in seen or seen.add(n))]

    blocked = [n for n in ordered if n in PROTECTED_TENANTS]
    if blocked:
        sys.exit(
            f"error: refusing to run — protected tenant(s): {', '.join(blocked)}.\n"
            "       Protected tenants cannot be targeted, even explicitly."
        )

    missing = [n for n in ordered if n not in known]
    if missing:
        sys.exit(
            f"error: tenant(s) not found: {', '.join(missing)}.\n"
            f"       Known tenants: {', '.join(sorted(known)) or '(none)'}"
        )

    return [(n, known[n]) for n in ordered]


def _resolve_clients(
    sb: Client,
    tenant: str,
    tenant_id: str,
    only: Optional[str],
    strict: bool = True,
) -> List[dict]:
    """Clients under `tenant`, optionally narrowed to one source_dataset.

    The tenant_id filter is the isolation boundary. `only` narrows within that
    set — it is never used as a standalone lookup key, because source_dataset
    is only unique per tenant (see clients_tenant_source_unique in schema.sql),
    so a bare name lookup could resolve to another tenant's client.

    `strict` controls what an unmatched `only` means. With ONE tenant named it
    is fatal — the name is simply wrong and there is nothing to do. With
    SEVERAL named the intent is "this client wherever it appears among these
    tenants", so a tenant that lacks it is skipped instead. Without that,
    `--tenant all --client clientA` could never run, since migratehq has no
    clients at all. The caller still errors if NO tenant matched.
    """
    resp = (
        sb.table("clients")
        .select("id, source_dataset, display_name")
        .eq("tenant_id", tenant_id)
        .execute()
    )
    clients = resp.data or []

    if only is None:
        return sorted(clients, key=lambda c: c.get("source_dataset") or "")

    matched = [c for c in clients if c.get("source_dataset") == only]
    if not matched and strict:
        names = ", ".join(sorted(c.get("source_dataset") or "?" for c in clients)) or "(none)"
        sys.exit(
            f"error: no client '{only}' under tenant '{tenant}'.\n"
            f"       Clients in this tenant: {names}\n"
            f"       (If '{only}' exists under a different tenant, this script will "
            f"not touch it — that is intentional.)"
        )
    return matched


def _supabase_counts(sb: Client, clients: List[dict]) -> Tuple[Dict[str, List[str]], Dict[str, int]]:
    """client_id -> its run_ids, and client_id -> its mapping_results count."""
    runs_by_client: Dict[str, List[str]] = defaultdict(list)
    results_by_client: Dict[str, int] = defaultdict(int)

    for batch in _chunks([c["id"] for c in clients]):
        resp = (
            sb.table("mapping_runs")
            .select("run_id, client_id")
            .in_("client_id", batch)
            .execute()
        )
        for row in resp.data or []:
            runs_by_client[row["client_id"]].append(row["run_id"])

    for client_id, run_ids in runs_by_client.items():
        for batch in _chunks(run_ids):
            resp = (
                sb.table("mapping_results")
                .select("id", count="exact")
                .in_("run_id", batch)
                .execute()
            )
            results_by_client[client_id] += resp.count or 0

    return runs_by_client, results_by_client


def _bigquery_counts(bq, bqmod, run_ids: List[str]) -> Tuple[int, int]:
    """(runs, results) present in BigQuery for these run_ids."""
    if not run_ids:
        return 0, 0
    params = [bqmod.ArrayQueryParameter("rids", "STRING", run_ids)]
    cfg = bqmod.QueryJobConfig(query_parameters=params)
    try:
        runs = list(bq.query(
            f"SELECT COUNT(*) AS c FROM `{BQ_RUNS_TABLE}` WHERE run_id IN UNNEST(@rids)", job_config=cfg
        ).result())[0].c
        cfg = bqmod.QueryJobConfig(query_parameters=params)
        results = list(bq.query(
            f"SELECT COUNT(*) AS c FROM `{BQ_RESULTS_TABLE}` WHERE run_id IN UNNEST(@rids)", job_config=cfg
        ).result())[0].c
    except Exception as exc:
        raise BigQueryUnavailable(str(exc))
    return runs, results


# ── deletion ──────────────────────────────────────────────────────────────────

def _rule_counts(sb: Client, client_ids: List[str]) -> Dict[str, int]:
    """Cache-rule rows in scope. Tolerates an un-migrated database."""
    out = {"mapping_rules": 0, "mapping_rule_events": 0}
    if not client_ids:
        return out
    rule_ids: List[str] = []
    for batch in _chunks(client_ids):
        try:
            rows = (sb.table("mapping_rules").select("id").in_("client_id", batch).execute().data) or []
        except Exception as exc:
            if _is_missing_table(exc):
                return out
            raise
        rule_ids.extend(r["id"] for r in rows)
    out["mapping_rules"] = len(rule_ids)
    for batch in _chunks(rule_ids):
        try:
            resp = sb.table("mapping_rule_events").select("*", count="exact").in_("rule_id", batch).execute()
            out["mapping_rule_events"] += resp.count or 0
        except Exception as exc:
            if _is_missing_table(exc):
                break
            raise
    return out


def _related_counts(sb: Client, run_ids: List[str]) -> Dict[str, int]:
    """
    Counts for the lifecycle tables added in prompt28.

    Reported separately from the per-client table to keep that readable, and
    tolerant of a database where the migration has not been applied yet — the
    schema.sql DDL has to be run by hand.
    """
    out = {"exports": 0, "export_mappings": 0, "status_history": 0}
    if not run_ids:
        return out

    mapping_ids: List[str] = []
    for batch in _chunks(run_ids):
        rows = (sb.table("mapping_results").select("id").in_("run_id", batch).execute().data) or []
        mapping_ids.extend(r["id"] for r in rows)

    export_ids: List[str] = []
    for batch in _chunks(run_ids):
        try:
            rows = (sb.table("exports").select("id").in_("run_id", batch).execute().data) or []
        except Exception as exc:
            if _is_missing_table(exc):
                return out
            raise
        export_ids.extend(r["id"] for r in rows)
    out["exports"] = len(export_ids)

    for table, column, ids, key in (
        ("mapping_status_history", "mapping_id", mapping_ids, "status_history"),
        ("export_mappings",        "mapping_id", mapping_ids, "export_mappings"),
    ):
        for batch in _chunks(ids):
            try:
                resp = sb.table(table).select("*", count="exact").in_(column, batch).execute()
                out[key] += resp.count or 0
            except Exception as exc:
                if _is_missing_table(exc):
                    break
                raise
    return out


def _bq_table_exists(bq, table: str) -> bool:
    """The revocation ledger only exists once something has been revoked."""
    try:
        bq.get_table(table)
        return True
    except Exception:
        return False


def _bigquery_revocation_count(bq, bqmod, run_ids: List[str]) -> int:
    if not run_ids or not _bq_table_exists(bq, BQ_REVOCATIONS_TABLE):
        return 0
    total = 0
    for batch in _chunks(run_ids):
        cfg = bqmod.QueryJobConfig(
            query_parameters=[bqmod.ArrayQueryParameter("rids", "STRING", batch)]
        )
        total += list(bq.query(
            f"SELECT COUNT(*) AS c FROM `{BQ_REVOCATIONS_TABLE}` WHERE run_id IN UNNEST(@rids)",
            job_config=cfg,
        ).result())[0].c
    return total


def _is_dml_forbidden(exc: Exception) -> bool:
    """
    BigQuery DML requires a billing account; the free tier rejects it outright
    with 403 billingNotEnabled. This project is currently on the free tier, so
    the DML path below never succeeds here — see _delete_bigquery.
    """
    text = str(exc).lower()
    return "billingnotenabled" in text or "dml queries are not allowed" in text


def _delete_bigquery(bq, bqmod, run_ids: List[str]) -> None:
    """Revocations, then results, then runs — mirroring the Supabase order.

    BigQuery enforces no FK, so the order is cosmetic here, but keeping the two
    paths identical means one less difference to reason about. The revocation
    ledger is skipped when it does not exist yet (nothing has been revoked).

    TWO STRATEGIES. DML DELETE is tried first because it is surgical. On a
    free-tier project DML is refused outright (403 billingNotEnabled), so the
    fallback rewrites the table through a query job with WRITE_TRUNCATE,
    keeping every row that is NOT in scope. A query-with-destination is not
    DML and is permitted on the free tier.

    The rewrite is atomic — BigQuery materialises the result and swaps the
    destination only on success — so an interrupted job cannot leave the table
    truncated. It does rewrite the whole table, which is acceptable for a
    dev/demo reset and is why this is a fallback rather than the default.
    """
    tables = [BQ_RESULTS_TABLE, BQ_RUNS_TABLE]
    if _bq_table_exists(bq, BQ_REVOCATIONS_TABLE):
        tables.insert(0, BQ_REVOCATIONS_TABLE)

    try:
        for batch in _chunks(run_ids):
            params = [bqmod.ArrayQueryParameter("rids", "STRING", batch)]
            for table in tables:
                cfg = bqmod.QueryJobConfig(query_parameters=params)
                bq.query(f"DELETE FROM `{table}` WHERE run_id IN UNNEST(@rids)", job_config=cfg).result()
        return
    except Exception as exc:
        if not _is_dml_forbidden(exc):
            raise
        print("  note: BigQuery DML unavailable on this project (free tier) — "
              "falling back to a rewrite without the in-scope rows.")

    for table in tables:
        cfg = bqmod.QueryJobConfig(
            query_parameters=[bqmod.ArrayQueryParameter("rids", "STRING", run_ids)],
            destination=table,
            write_disposition=bqmod.WriteDisposition.WRITE_TRUNCATE,
        )
        bq.query(
            f"SELECT * FROM `{table}` WHERE run_id NOT IN UNNEST(@rids)",
            job_config=cfg,
        ).result()


def _delete_supabase(sb: Client, run_ids: List[str], client_ids: List[str]) -> None:
    """
    Ordered by FK dependency.

    client_ids is separate from run_ids on purpose: mapping_rules is scoped to
    a CLIENT, not to a run, so a run-scoped delete alone never touches it.

    mapping_status_history and export_mappings both cascade from
    mapping_results, and export_mappings also cascades from exports — but the
    deletes are issued explicitly rather than relying on ON DELETE CASCADE, so
    the row counts reported in the summary are real rather than inferred.

    exports references mapping_runs(run_id), so it must go before the runs.
    """
    mapping_ids: List[str] = []
    for batch in _chunks(run_ids):
        rows = (
            sb.table("mapping_results").select("id").in_("run_id", batch).execute().data
        ) or []
        mapping_ids.extend(r["id"] for r in rows)

    export_ids: List[str] = []
    for batch in _chunks(run_ids):
        rows = (
            sb.table("exports").select("id").in_("run_id", batch).execute().data
        ) or []
        export_ids.extend(r["id"] for r in rows)

    # mapping_status_history is protected by an append-only trigger that also
    # fires for FK cascades, so neither a direct DELETE nor a cascade from
    # mapping_results can remove it. purge_run_history() is the one sanctioned
    # escape hatch (see MIGRATION 2 in backend/schema.sql); it sets a
    # transaction-local flag the trigger honours.
    for run_id in run_ids:
        try:
            sb.rpc("purge_run_history", {"p_run_id": run_id}).execute()
        except Exception as exc:
            if _is_missing_function(exc):
                raise SystemExit(
                    "error: purge_run_history() not found in the database.\n"
                    "       Apply MIGRATION 2 from backend/schema.sql first — without it the\n"
                    "       append-only trigger blocks every mapping_results delete and this\n"
                    "       reset cannot complete.\n"
                    "       NOTE: BigQuery rows for this scope have ALREADY been deleted at\n"
                    "       this point; re-run after applying the migration to finish."
                )
            raise

    for batch in _chunks(mapping_ids):
        _safe_delete(sb, "export_mappings", "mapping_id", batch)
    for batch in _chunks(export_ids):
        _safe_delete(sb, "export_mappings", "export_id", batch)
    for batch in _chunks(run_ids):
        sb.table("mapping_results").delete().in_("run_id", batch).execute()
    for batch in _chunks(run_ids):
        _safe_delete(sb, "exports", "run_id", batch)
    for batch in _chunks(run_ids):
        sb.table("mapping_runs").delete().in_("run_id", batch).execute()

    # mapping_rules is CLIENT-scoped, so none of the run-scoped deletes above
    # reach it, and the script keeps client rows by design (so a re-run reuses
    # the same client_id) — which means the ON DELETE CASCADE from clients
    # never fires either. Without this, a reset leaves the client's cache rules
    # in place and the next pipeline run auto-applies rules from before the
    # reset, which is the opposite of a clean pre-run state. (Finding F2,
    # prompt38.)
    #
    # mapping_rule_events cascades from mapping_rules, but is deleted
    # explicitly first so the reported counts are real rather than inferred —
    # same convention as the rest of this function.
    rule_ids: List[str] = []
    for batch in _chunks(client_ids):
        try:
            rows = (
                sb.table("mapping_rules").select("id").in_("client_id", batch).execute().data
            ) or []
        except Exception as exc:
            if _is_missing_table(exc):
                rows = []          # Migration 3 not applied — nothing to purge
            else:
                raise
        rule_ids.extend(r["id"] for r in rows)

    for batch in _chunks(rule_ids):
        _safe_delete(sb, "mapping_rule_events", "rule_id", batch)
    for batch in _chunks(client_ids):
        _safe_delete(sb, "mapping_rules", "client_id", batch)


def _safe_delete(sb: Client, table: str, column: str, values: List[str]) -> None:
    """
    Delete, tolerating a table that has not been migrated yet.

    The lifecycle migration (schema.sql, prompt28) has to be applied by hand,
    so this script must keep working on a database where the new tables do not
    exist. A missing table is skipped; every other error propagates.
    """
    if not values:
        return
    try:
        sb.table(table).delete().in_(column, values).execute()
    except Exception as exc:
        if _is_missing_table(exc):
            return
        raise


def _is_missing_table(exc: Exception) -> bool:
    text = str(exc).lower()
    return "does not exist" in text or "could not find the table" in text or "pgrst205" in text


def _is_missing_function(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "could not find the function" in text
        or "pgrst202" in text
        or ("does not exist" in text and "function" in text)
    )


# ── reporting ─────────────────────────────────────────────────────────────────

def _print_plan(rows: List[dict], bq_ok: bool) -> None:
    print(f"  {'TENANT / CLIENT':<30} {'SB runs':>8} {'SB maps':>8} {'BQ runs':>8} {'BQ maps':>8}")
    print(f"  {'-' * 30} {'-' * 8} {'-' * 8} {'-' * 8} {'-' * 8}")
    for r in rows:
        bq_runs = f"{r['bq_runs']:>8}" if bq_ok else f"{'?':>8}"
        bq_maps = f"{r['bq_maps']:>8}" if bq_ok else f"{'?':>8}"
        print(f"  {r['label']:<30} {r['sb_runs']:>8} {r['sb_maps']:>8} {bq_runs} {bq_maps}")
    print(f"  {'-' * 30} {'-' * 8} {'-' * 8} {'-' * 8} {'-' * 8}")
    t_sb_runs = sum(r["sb_runs"] for r in rows)
    t_sb_maps = sum(r["sb_maps"] for r in rows)
    t_bq_runs = sum(r["bq_runs"] for r in rows)
    t_bq_maps = sum(r["bq_maps"] for r in rows)
    total_label = f"TOTAL ({len(rows)} clients)"
    if bq_ok:
        print(f"  {total_label:<30} {t_sb_runs:>8} {t_sb_maps:>8} {t_bq_runs:>8} {t_bq_maps:>8}\n")
    else:
        print(f"  {total_label:<30} {t_sb_runs:>8} {t_sb_maps:>8} {'?':>8} {'?':>8}\n")


# ============================================================================
# REPLAY MODE — restore a captured demo baseline (SUPABASE ONLY)
# ============================================================================
# `--mode replay` resets ONE demo client and then re-inserts the run captured by
# scripts/snapshot_demo_data.py, so a demo can start from a known pre-review
# state without re-running the pipeline. It is deliberately far narrower than
# delete mode: two named clients under one named tenant, nothing else, no
# `--tenant all`, no wildcard.
#
# NO BIGQUERY ON THIS PATH — not even a client object is constructed.
# Delete mode contacts BigQuery first because BigQuery has no tenant column, so
# its rows can only be identified through Supabase's run_id -> client -> tenant
# chain; deleting Supabase first would strand them unidentifiable. Replay does
# not delete anything from BigQuery, so that ordering constraint simply does not
# apply, and the argument for touching BigQuery at all disappears with it.
#
# The consequence is stated rather than hidden: BigQuery KEEPS the rows of the
# runs replay removes from Supabase, and the restored run (which carries a NEW
# run_id, see _replay_new_run_id) is absent from BigQuery entirely until a human
# approves something through the API. For demo tooling that is the right trade —
# the alternative is a demo script mutating the Truth Layer.

REPLAY_TENANT = "tenant_2"
REPLAY_CLIENTS = ("clientA", "clientB")

_SNAPSHOT_DIR = os.path.join(_REPO_ROOT, "demo", "snapshots")
_UPLOADS_DIR = os.path.join(_REPO_ROOT, "schema-mapping", "data", "uploads")

# Mirrors backend/main.py: the private bucket, and the
# {tenant_id}/{run_id}/source{ext} convention the ingest path writes and
# GET /api/runs/{run_id}/source-url reads back.
_SOURCE_STORAGE_BUCKET = "source-files"
_SOURCE_CONTENT_TYPES = {
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pdf": "application/pdf",
}

# The only two values the pipeline's approval gate can produce
# (schema-mapping/bigquery_loader.py:425). Anything else in a snapshot's
# original_status means the file was hand-edited or written by a different gate,
# and replay refuses rather than inserting it.
_ORIGINAL_STATUSES = {"pending_review", "approved"}

# Restored as NULL rather than copied verbatim. Every one is a POST-INGEST fact
# AND a foreign key into a row replay does not restore. See
# _replay_result_payloads() for the full reasoning.
_NULLED_ON_REPLAY = ("exported_at", "export_id", "rule_id")


def _replay_exit(*lines) -> None:
    sys.exit("\n".join(lines))


def _replay_check_args(args) -> Tuple[str, str]:
    """Validate the replay scope. Refuses loudly; never silently no-ops.

    Replay INSERTS rows, so its blast radius has to be a whitelist rather than
    a filter: an unrecognised (tenant, client) pair is a mistake, not an empty
    result set. --tenant keeps reset's 'migratehq' default, so a bare
    `--mode replay --client clientA` is refused here rather than resolving to a
    tenant that has no clients at all.
    """
    raw = (args.tenant or "").strip()

    if raw.lower() == "all":
        _replay_exit(
            "error: --tenant all is not allowed with --mode replay.",
            "       Replay writes rows; it targets exactly one tenant and one client.",
            "       Use: --mode replay --tenant {} --client {}".format(
                REPLAY_TENANT, "|".join(REPLAY_CLIENTS)),
        )

    names = [n.strip() for n in raw.split(",") if n.strip()]
    if len(names) != 1:
        _replay_exit(
            "error: --mode replay takes exactly one --tenant (got {!r}).".format(raw),
            "       Use: --mode replay --tenant {} --client {}".format(
                REPLAY_TENANT, "|".join(REPLAY_CLIENTS)),
        )
    tenant = names[0]

    # Redundant with _resolve_tenants(), which also refuses protected tenants —
    # kept here so the guarantee holds before any connection is opened, exactly
    # as the delete path does it.
    if tenant in PROTECTED_TENANTS:
        _replay_exit(
            "error: refusing to run — protected tenant: {}.".format(tenant),
            "       Protected tenants cannot be targeted, even explicitly.",
        )

    if tenant != REPLAY_TENANT:
        _replay_exit(
            "error: --mode replay only supports tenant '{}' (got '{}').".format(
                REPLAY_TENANT, tenant),
            "       The demo baselines in demo/snapshots/ were captured under '{}';"
            .format(REPLAY_TENANT),
            "       clientA and clientB also exist under tenant_1 as DIFFERENT client",
            "       rows with different runs, so replaying a tenant_2 snapshot there",
            "       would restore one tenant's data into another. Refusing.",
            "       (--tenant defaults to '{}' — pass --tenant {} explicitly.)".format(
                DEFAULT_TENANT, REPLAY_TENANT),
        )

    if not args.client:
        _replay_exit(
            "error: --mode replay requires --client ({}).".format("|".join(REPLAY_CLIENTS)),
            "       There is no 'every client' replay: each client has its own snapshot.",
        )

    if args.client not in REPLAY_CLIENTS:
        _replay_exit(
            "error: --mode replay only supports --client {} (got '{}').".format(
                " or ".join(REPLAY_CLIENTS), args.client),
            "       Only those two have a captured baseline in demo/snapshots/.",
        )

    return tenant, args.client


def _load_snapshot(tenant: str, client: str) -> Tuple[dict, str]:
    """Read + validate demo/snapshots/{client}.json BEFORE anything destructive.

    Every check here runs before the first delete, so a bad or missing snapshot
    can never leave the client wiped with nothing to restore.
    """
    path = os.path.join(_SNAPSHOT_DIR, "{}.json".format(client))
    if not os.path.isfile(path):
        _replay_exit(
            "error: no snapshot at {}".format(os.path.relpath(path, _REPO_ROOT)),
            "       Capture one first:",
            "         python scripts/snapshot_demo_data.py --tenant {} --client {} "
            "--run-id <uuid>".format(tenant, client),
        )

    try:
        with open(path) as fh:
            snapshot = json.load(fh)
    except ValueError as exc:
        _replay_exit(
            "error: {} is not valid JSON: {}".format(os.path.relpath(path, _REPO_ROOT), exc),
        )

    if not isinstance(snapshot, dict):
        _replay_exit("error: {} is not a snapshot object.".format(path))

    # The snapshot filename is keyed on client name only, so a snapshot taken
    # under tenant_1 would sit at exactly this path and look identical. The
    # embedded tenant/client fields are the only thing that distinguishes them.
    if snapshot.get("tenant") != tenant or snapshot.get("client") != client:
        _replay_exit(
            "error: snapshot/CLI mismatch — refusing to replay.",
            "       file        : {}".format(os.path.relpath(path, _REPO_ROOT)),
            "       file says   : tenant={!r} client={!r}".format(
                snapshot.get("tenant"), snapshot.get("client")),
            "       CLI asked   : tenant={!r} client={!r}".format(tenant, client),
            "       Snapshot filenames are keyed on client name alone, so a snapshot",
            "       of the OTHER tenant's client of the same name lives at this path.",
        )

    run_row = snapshot.get("mapping_runs")
    if not isinstance(run_row, dict) or not run_row:
        _replay_exit("error: snapshot has no usable 'mapping_runs' object.")

    rows = snapshot.get("mapping_results")
    if not isinstance(rows, list) or not rows:
        _replay_exit("error: snapshot has no 'mapping_results' rows — nothing to restore.")

    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            _replay_exit("error: mapping_results[{}] is not an object.".format(i))
        # THE central invariant of this feature. `status` in the snapshot is the
        # CURRENT (post-review) value; `original_status` is the pipeline's. If
        # original_status is absent or unrecognised, restoring `status` instead
        # would look successful and silently defeat the whole point, so this is
        # a hard refusal rather than a fallback.
        if "original_status" not in row:
            _replay_exit(
                "error: mapping_results[{}] ({!r}) has no 'original_status'.".format(
                    i, row.get("source_column")),
                "       This snapshot predates the reconstruction and cannot be replayed;",
                "       restoring its 'status' would replay the post-review state.",
            )
        if row["original_status"] not in _ORIGINAL_STATUSES:
            _replay_exit(
                "error: mapping_results[{}] ({!r}) has original_status={!r}.".format(
                    i, row.get("source_column"), row["original_status"]),
                "       The pipeline gate only ever produces {}.".format(
                    " or ".join(sorted(_ORIGINAL_STATUSES))),
            )

    return snapshot, path


def _snapshot_source_files(snapshot: dict) -> List[Tuple[str, str]]:
    """[(absolute stored path, filename)] for every file in the snapshot.

    Every entry in `files` is restored — never just the first. clientB's
    baseline is a PDF run with TWO files: source.pdf (what the signed-URL flow
    serves) and source_extracted.csv (what export_run() actually reads). `files`
    is always a list, including for clientA's single CSV, so there is no shape
    to branch on.
    """
    sf = snapshot.get("source_file") or {}
    out: List[Tuple[str, str]] = []
    for entry in sf.get("files") or []:
        rel = entry.get("stored_path")
        if not rel:
            continue
        name = entry.get("original_filename") or os.path.basename(rel)
        out.append((os.path.join(_REPO_ROOT, rel), name))
    return out


def _check_source_files(snapshot: dict) -> List[Tuple[str, str]]:
    """Verify the stored source files exist on disk, before deleting anything."""
    files = _snapshot_source_files(snapshot)
    missing = [p for p, _ in files if not os.path.isfile(p)]
    if missing:
        _replay_exit(
            "error: snapshot references source file(s) that are not on disk:",
            *["         {}".format(os.path.relpath(p, _REPO_ROOT)) for p in missing]
        )
    return files


def _restore_source_files(files: List[Tuple[str, str]], run_id: str) -> Tuple[List[str], Optional[str]]:
    """Copy the snapshot's source file(s) into uploads/{run_id}/.

    Returns (filenames written, path of the PRIMARY file). The primary is the
    one whose stem is exactly 'source' — source.pdf for a PDF run, not the
    derived source_extracted.csv — matching what the ingest path mirrors to
    Storage.
    """
    if not files:
        return [], None
    dest_dir = os.path.join(_UPLOADS_DIR, run_id)
    os.makedirs(dest_dir, exist_ok=True)
    written: List[str] = []
    primary: Optional[str] = None
    for src, name in files:
        dst = os.path.join(dest_dir, name)
        shutil.copy2(src, dst)
        written.append(name)
        if os.path.splitext(name)[0] == "source":
            primary = dst
    return written, primary


def _upload_source_to_storage(sb: Client, tenant_id: str, run_id: str,
                              primary: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Re-mirror the primary source file to the private Storage bucket.

    Returns (source_storage_path, error). Non-fatal, exactly like the ingest
    path it mirrors: a missing bucket leaves the path NULL and the Source link
    404s for this run, but the replay itself still succeeds.

    WHY NOT RESTORE THE SNAPSHOT'S source_storage_path VERBATIM: it points at
    {old_tenant_id}/{ORIGINAL run_id}/source.csv. The restored run has a new
    run_id, so that path is a claim about an object belonging to a run that no
    longer exists — GET /api/runs/{run_id}/source-url would mint a signed URL
    into another run's folder, and would break outright the moment that object
    is cleaned up. Re-uploading under the new run_id makes the column true
    again; NULL (the fallback) makes it honestly absent (404 "No source file
    stored for run"). A stale-but-plausible path is the one option with a wrong
    answer, so it is the one option not taken.
    """
    if not primary:
        return None, "snapshot contains no primary source file"
    ext = os.path.splitext(primary)[1].lower()
    path = "{}/{}/source{}".format(tenant_id, run_id, ext)
    try:
        with open(primary, "rb") as fh:
            data = fh.read()
        sb.storage.from_(_SOURCE_STORAGE_BUCKET).upload(
            path,
            data,
            file_options={
                "content-type": _SOURCE_CONTENT_TYPES.get(ext, "application/octet-stream"),
                "upsert": "true",
            },
        )
        return path, None
    except Exception as exc:
        return None, str(exc)


def _replay_run_payload(snapshot: dict, client_id: str, run_id: str,
                        storage_path: Optional[str]) -> dict:
    """The mapping_runs row to insert.

    Three fields are NOT taken from the snapshot:

      * `id` is DROPPED. It is the surrogate Supabase PK, not the run identity —
        the business key is the separate `run_id` column. Re-inserting the
        captured `id` would collide the moment a replay is run twice.
      * `client_id` is REPLACED with the live lookup. The captured value points
        at whichever clientA the snapshot was taken from; it is cross-checked
        against the live one by the caller and warned about, never trusted.
      * `source_storage_path` is REPLACED (see _upload_source_to_storage).

    Everything else is verbatim, including `created_at`: the baseline is meant
    to be a specific captured run, and a stable timestamp keeps repeated
    replays byte-identical rather than drifting each time.
    """
    payload = {k: v for k, v in snapshot["mapping_runs"].items()
               if k not in ("id", "client_id", "source_storage_path", "run_id")}
    payload["run_id"] = run_id
    payload["client_id"] = client_id
    payload["source_storage_path"] = storage_path
    return payload


def _replay_result_payloads(snapshot: dict, run_id: str) -> List[dict]:
    """The mapping_results rows to insert, at their PIPELINE-TIME state.

    status  <- original_status, NEVER the snapshot's `status`
    --------------------------------------------------------
    `status` is the current, post-review value; `original_status` is what the
    pipeline's own gate assigned at ingest. Copying the wrong one restores an
    already-reviewed queue and defeats the feature while looking perfectly
    healthy. So `status` is dropped from the copied dict FIRST — it cannot
    survive into the payload by accident — and reassigned from original_status,
    with the result cross-checked by the caller.

    exported_at / export_id / rule_id  <- NULL
    ------------------------------------------
    Two independent reasons, either of which alone is sufficient:

      1. They are post-ingest facts. At pipeline time every row had them NULL,
         exactly like `status`. The snapshots carry a populated exported_at +
         export_id on their approved rows; restoring those verbatim would start
         the demo in an already-exported state, so the export delta claim
         (schema.sql: UPDATE ... WHERE exported_at IS NULL) would claim nothing
         and revoke would land straight on its already-exported 409 path.
      2. They are foreign keys into rows replay does not restore.
         schema.sql: `export_id UUID REFERENCES exports(id)` and
         `rule_id UUID REFERENCES mapping_rules(id)`. The reset step deletes
         this client's exports and mapping_rules, so the captured ids are
         dangling and the INSERT would be rejected outright.

    Verbatim, deliberately: target_column, confidence, layer — untouched L1/L2
    output — and flagged_for_review, which the same gate assigns at ingest
    (bigquery_loader.py:422) and is therefore also an original pipeline value.
    """
    payloads: List[dict] = []
    for row in snapshot["mapping_results"]:
        payload = {k: v for k, v in row.items()
                   if k not in ("original_status", "status", "run_id", "id")}
        payload["run_id"] = run_id
        payload["status"] = row["original_status"]
        for field in _NULLED_ON_REPLAY:
            payload[field] = None
        payloads.append(payload)

    # Executable assertion, not a comment: the inserted statuses must be the
    # snapshot's original_status column, position for position.
    expected = [r["original_status"] for r in snapshot["mapping_results"]]
    actual = [p["status"] for p in payloads]
    if actual != expected:
        _replay_exit(
            "internal error: replay payload status does not match original_status.",
            "       expected: {}".format(expected),
            "       built   : {}".format(actual),
        )
    return payloads


def _status_counts(rows: List[dict], key: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for row in rows:
        out[row.get(key)] = out.get(row.get(key), 0) + 1
    return out


def _verify_replay(sb: Client, client_id: str, run_id: str,
                   snapshot: dict) -> List[str]:
    """Re-read what was written and compare it to the snapshot. Trusts nothing."""
    problems: List[str] = []

    runs = (sb.table("mapping_runs").select("run_id").eq("client_id", client_id)
            .execute().data) or []
    if len(runs) != 1:
        problems.append("expected exactly 1 run for the client, found {} ({})".format(
            len(runs), ", ".join(r["run_id"] for r in runs)))
    elif runs[0]["run_id"] != run_id:
        problems.append("the client's only run is {}, expected {}".format(
            runs[0]["run_id"], run_id))

    live = (sb.table("mapping_results").select("*").eq("run_id", run_id)
            .execute().data) or []
    expected_rows = snapshot["mapping_results"]
    if len(live) != len(expected_rows):
        problems.append("expected {} mapping_results row(s), found {}".format(
            len(expected_rows), len(live)))

    want_status = _status_counts(expected_rows, "original_status")
    got_status = _status_counts(live, "status")
    if want_status != got_status:
        problems.append("status distribution {} != expected {}".format(got_status, want_status))

    by_col = {r.get("source_column"): r for r in live}
    for row in expected_rows:
        col = row.get("source_column")
        got = by_col.get(col)
        if got is None:
            problems.append("missing source_column {!r}".format(col))
            continue
        if got.get("target_column") != row.get("target_column"):
            problems.append("{!r}: target_column {!r} != {!r}".format(
                col, got.get("target_column"), row.get("target_column")))
        if got.get("layer") != row.get("layer"):
            problems.append("{!r}: layer {!r} != {!r}".format(
                col, got.get("layer"), row.get("layer")))
        a, b = got.get("confidence"), row.get("confidence")
        if (a is None) != (b is None) or (a is not None and abs(a - b) > 1e-9):
            problems.append("{!r}: confidence {!r} != {!r}".format(col, a, b))
        if got.get("flagged_for_review") != row.get("flagged_for_review"):
            problems.append("{!r}: flagged_for_review {!r} != {!r}".format(
                col, got.get("flagged_for_review"), row.get("flagged_for_review")))
        for field in _NULLED_ON_REPLAY:
            if got.get(field) is not None:
                problems.append("{!r}: {} is {!r}, expected NULL".format(col, field, got.get(field)))

    return problems


def _run_replay(args) -> int:
    """`--mode replay`. Reset one demo client in Supabase, then restore its
    captured baseline. Never opens a BigQuery connection."""
    tenant, client = _replay_check_args(args)
    snapshot, snap_path = _load_snapshot(tenant, client)
    source_files = _check_source_files(snapshot)

    sb = _connect_supabase()
    # Reused rather than reimplemented: this is where 'tenant not found' and the
    # protected-tenant refusal live.
    tenant_name, tenant_id = _resolve_tenants(sb, tenant)[0]
    clients = _resolve_clients(sb, tenant_name, tenant_id, client, strict=True)
    if len(clients) != 1:
        _replay_exit(
            "error: ({!r}, {!r}) resolved to {} client rows — refusing to guess.".format(
                tenant_name, client, len(clients)),
            *["         client_id={}".format(c["id"]) for c in clients]
        )
    client_row = clients[0]
    client_id = client_row["id"]
    label = "{}/{}".format(tenant_name, client)

    runs_by_client, results_by_client = _supabase_counts(sb, clients)
    run_ids = runs_by_client.get(client_id, [])
    related = _related_counts(sb, run_ids)
    rules = _rule_counts(sb, [client_id])

    rows = snapshot["mapping_results"]
    want_status = _status_counts(rows, "original_status")
    now_status = _status_counts(rows, "status")

    print("\nMode   : REPLAY (Supabase only — BigQuery is not contacted)")
    print("Tenant : {}  (tenant_id {})".format(tenant_name, tenant_id))
    print("Client : {}  (client_id {})".format(client, client_id))
    print("Snapshot: {}".format(os.path.relpath(snap_path, _REPO_ROOT)))
    print("          captured {} from run {}".format(
        snapshot.get("captured_at"), snapshot.get("source_run_id")))

    # The snapshot's client_id is a CROSS-CHECK only. The live lookup wins,
    # because a restore has to land in the database it is actually pointed at.
    snap_client_id = snapshot.get("client_id")
    if snap_client_id and snap_client_id != client_id:
        print("\n  *** WARNING: client_id MISMATCH ***")
        print("      snapshot records : {}".format(snap_client_id))
        print("      live lookup gives: {}".format(client_id))
        print("      Using the LIVE value. The snapshot was probably taken against a")
        print("      different database, or the client row was recreated. If this is")
        print("      unexpected, stop and re-capture the snapshot.\n")

    print("\n  Step 1  DELETE (Supabase only) — {}".format(label))
    print("            {} run(s), {} mapping row(s), {} export(s), {} export membership row(s),"
          .format(len(run_ids), results_by_client.get(client_id, 0),
                  related["exports"], related["export_mappings"]))
    print("            {} status-history row(s), {} cache rule(s), {} rule event(s)"
          .format(related["status_history"], rules["mapping_rules"], rules["mapping_rule_events"]))
    print("            (history is purged first via purge_run_history(); the append-only")
    print("             trigger blocks the mapping_results delete otherwise)")
    print("  Step 2  INSERT 1 mapping_runs row + {} mapping_results row(s) under a NEW run_id"
          .format(len(rows)))
    print("            status from original_status : {}".format(want_status))
    print("            (snapshot's current status  : {} — NOT restored)".format(now_status))
    print("            exported_at / export_id / rule_id : NULL")
    print("  Step 3  COPY source file(s) -> schema-mapping/data/uploads/<new_run_id>/")
    print("            {}".format(", ".join(n for _, n in source_files) or "(none)"))
    print("  BigQuery: NOT TOUCHED. Rows for the deleted run(s) remain there; the")
    print("            restored run is absent from BigQuery until a human approves.\n")

    if args.dry_run:
        print("Dry run — nothing was deleted, inserted or copied.\n")
        return 0

    if not args.yes:
        answer = input(
            "Reset {} in Supabase ({} run(s), {} mapping row(s)) and restore the {} "
            "snapshot ({} row(s))? [y/N] ".format(
                label, len(run_ids), results_by_client.get(client_id, 0), client, len(rows))
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted — nothing was deleted or inserted.\n")
            return 1

    # ── Step 1: reset. Same function the delete mode uses, so the purge-history
    # ordering and the missing-migration error live in exactly one place.
    try:
        _delete_supabase(sb, run_ids, [client_id])
    except SystemExit:
        raise
    except Exception as exc:
        print("\nFAILED during the Supabase delete: {}\n"
              "  Nothing was restored. BigQuery was never touched.\n"
              "  Re-running this command is safe.\n".format(exc))
        return 1

    # ── Step 2: restore.
    run_id = str(uuid.uuid4())
    written, primary = _restore_source_files(source_files, run_id)
    storage_path, storage_err = _upload_source_to_storage(sb, tenant_id, run_id, primary)

    try:
        sb.table("mapping_runs").insert(
            _replay_run_payload(snapshot, client_id, run_id, storage_path)
        ).execute()
        sb.table("mapping_results").insert(
            _replay_result_payloads(snapshot, run_id)
        ).execute()
    except Exception as exc:
        print("\nFAILED during the restore insert: {}\n"
              "  The client was already RESET — it now has no runs. Re-run this\n"
              "  command to retry the restore; the reset step is idempotent.\n".format(exc))
        return 1

    problems = _verify_replay(sb, client_id, run_id, snapshot)

    print("\nReplay complete.")
    print("  tenant / client  : {}  (client_id {})".format(label, client_id))
    print("  deleted          : {} run(s), {} mapping row(s), {} export(s), {} history row(s)"
          .format(len(run_ids), results_by_client.get(client_id, 0),
                  related["exports"], related["status_history"]))
    print("  new run_id       : {}".format(run_id))
    print("  restored         : {} mapping_results row(s) at pipeline-time status {}"
          .format(len(rows), want_status))
    print("  source file(s)   : {} -> schema-mapping/data/uploads/{}/".format(
        ", ".join(written) or "(none)", run_id))
    if storage_path:
        print("  storage mirror   : {}".format(storage_path))
    else:
        print("  storage mirror   : NOT uploaded — source_storage_path is NULL "
              "(GET /api/runs/{run_id}/source-url will 404)")
        print("                     reason: {}".format(storage_err))
    print("  BigQuery         : not contacted")

    if problems:
        print("\n  WARNING — post-restore verification failed:")
        for line in problems:
            print("    - {}".format(line))
        print()
        return 1

    print("  verified         : run present, row count, statuses, target/confidence/layer,")
    print("                     flagged_for_review, and NULL export/rule fields all match\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reset seeded mapping data in BigQuery + Supabase (dev only).",
    )
    parser.add_argument(
        "--tenant",
        default=DEFAULT_TENANT,
        metavar="NAME",
        help=f"Tenant(s) to reset (default: {DEFAULT_TENANT}). Accepts one name, a "
             f"comma-separated list, or 'all' for the demo allowlist "
             f"({', '.join(sorted(DEMO_TENANTS - PROTECTED_TENANTS))}). "
             f"Always refused: {', '.join(sorted(PROTECTED_TENANTS))}.",
    )
    parser.add_argument(
        "--client",
        metavar="NAME",
        help="Reset a single client by source_dataset (e.g. clientA), applied "
             "within every named tenant. Omit to reset every client.",
    )
    parser.add_argument(
        "--mode",
        choices=("delete", "replay"),
        default="delete",
        help="delete (default): the existing BigQuery-then-Supabase reset, unchanged. "
             "replay: Supabase-only — reset ONE demo client and restore its captured "
             "baseline from demo/snapshots/ at pipeline-time status, without re-running "
             "the pipeline. Requires --tenant %s and --client %s."
             % (REPLAY_TENANT, "|".join(REPLAY_CLIENTS)),
    )
    parser.add_argument("--dry-run", action="store_true", help="Report what would be deleted, change nothing.")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt.")
    args = parser.parse_args()

    # Dispatched BEFORE _connect_supabase()/_connect_bigquery() so replay mode
    # never constructs a BigQuery client — the "no BigQuery on this path"
    # guarantee is structural, not a matter of which branches happen to run.
    if args.mode == "replay":
        return _run_replay(args)

    sb = _connect_supabase()
    tenants = _resolve_tenants(sb, args.tenant)

    # BigQuery is contacted up front, before anything is deleted anywhere, so a
    # credentials/permissions problem surfaces in the plan rather than halfway
    # through a reset. Fatal for a real run; a dry run still reports Supabase.
    bq = bqmod = None
    bq_error = None
    try:
        bq, bqmod = _connect_bigquery()
    except BigQueryUnavailable as exc:
        bq_error = str(exc)

    rows: List[dict] = []
    all_run_ids: List[str] = []
    all_client_ids: List[str] = []
    skipped: List[str] = []
    strict = len(tenants) == 1
    for tenant, tenant_id in tenants:
        clients = _resolve_clients(sb, tenant, tenant_id, args.client, strict=strict)
        if args.client and not clients:
            skipped.append(tenant)
            continue
        runs_by_client, results_by_client = _supabase_counts(sb, clients)
        for c in clients:
            run_ids = runs_by_client.get(c["id"], [])
            all_run_ids.extend(run_ids)
            all_client_ids.append(c["id"])
            bq_runs = bq_maps = 0
            if bq is not None:
                try:
                    bq_runs, bq_maps = _bigquery_counts(bq, bqmod, run_ids)
                except BigQueryUnavailable as exc:
                    bq_error = str(exc)
                    bq = None
            rows.append({
                "label": f"{tenant}/{c.get('source_dataset') or '?'}",
                "sb_runs": len(run_ids),
                "sb_maps": results_by_client.get(c["id"], 0),
                "bq_runs": bq_runs,
                "bq_maps": bq_maps,
            })

    # Every named tenant lacked the client — almost certainly a typo, so fail
    # loudly rather than reporting a clean "nothing to reset".
    if args.client and not rows:
        sys.exit(
            f"error: no client '{args.client}' under any named tenant "
            f"({', '.join(n for n, _ in tenants)})."
        )

    bq_ok = bq is not None
    scope = f"client '{args.client}'" if args.client else "all clients"
    print(f"\nTenants: {', '.join(n for n, _ in tenants)}")
    print(f"Scope  : {scope}")
    print("Action : DELETE mapping_results + mapping_runs from BigQuery, then Supabase")
    print("         (clients rows are kept)\n")
    if skipped:
        print(f"  note: no client '{args.client}' in {', '.join(skipped)} — skipped.\n")
    if not bq_ok:
        print(f"  ! BigQuery unavailable: {bq_error}\n")

    _print_plan(rows, bq_ok)

    related = _related_counts(sb, all_run_ids)
    bq_revocations = 0
    if bq_ok and all_run_ids:
        try:
            bq_revocations = _bigquery_revocation_count(bq, bqmod, all_run_ids)
        except Exception:
            pass
    rules = _rule_counts(sb, all_client_ids)
    print(f"  Also in scope — Supabase: {related['exports']} export(s), "
          f"{related['export_mappings']} export membership row(s), "
          f"{related['status_history']} status-history row(s)")
    print(f"                  Cache   : {rules['mapping_rules']} mapping rule(s), "
          f"{rules['mapping_rule_events']} rule event(s)")
    print(f"                  BigQuery: {bq_revocations} revocation ledger row(s)\n")

    if not all_run_ids:
        # A client can have cache rules with no runs at all — rules outlive the
        # runs that created them, and reset_demo_data keeps client rows. Bailing
        # here would leave those rules in place and the next pipeline run would
        # auto-apply pre-reset rules, which is the very thing F2 fixed. So the
        # rule purge still happens; only the run-scoped work is skipped.
        if any(rules.values()):
            if args.dry_run:
                print(f"Dry run — {rules['mapping_rules']} rule(s) would be cleared; "
                      "no runs to delete.\n")
                return 0
            if not args.yes:
                answer = input(f"No runs, but {rules['mapping_rules']} cache rule(s) "
                               "are in scope. Clear them? [y/N] ")
                if answer.strip().lower() not in ("y", "yes"):
                    print("Aborted — nothing was deleted.\n")
                    return 1
            _delete_supabase(sb, [], all_client_ids)
            left = _rule_counts(sb, all_client_ids)
            print(f"Cleared {rules['mapping_rules']} rule(s) and "
                  f"{rules['mapping_rule_events']} rule event(s); no runs to delete.")
            if any(left.values()):
                print(f"  WARNING: {left} still present.\n")
                return 1
            print()
            return 0
        print("Nothing to reset — these clients already have no runs.\n")
        return 0

    if args.dry_run:
        print("Dry run — nothing was deleted, in either system.\n")
        return 0

    # A real run must be able to reach BigQuery. Proceeding Supabase-only would
    # strand approved rows in BigQuery with no way left to identify them.
    if not bq_ok:
        print(
            "Aborted — refusing to delete from Supabase while BigQuery is unreachable.\n"
            "Supabase holds the only run_id -> tenant mapping; deleting it first would\n"
            "leave unidentifiable orphan rows in BigQuery. Fix BigQuery access and retry.\n"
        )
        return 1

    total_sb = sum(r["sb_maps"] for r in rows)
    total_bq = sum(r["bq_maps"] for r in rows)
    if not args.yes:
        answer = input(
            f"Delete {len(all_run_ids)} run(s) — {total_bq} BigQuery + {total_sb} Supabase "
            f"mapping row(s) — across {len(tenants)} tenant(s)? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted — nothing was deleted, in either system.\n")
            return 1

    # ── BigQuery first. See the module docstring for why the order is forced.
    try:
        _delete_bigquery(bq, bqmod, all_run_ids)
    except Exception as exc:
        print(
            f"\nFAILED during the BigQuery delete: {exc}\n"
            "  Supabase: UNTOUCHED — no Supabase rows were deleted.\n"
            "  BigQuery: possibly PARTIALLY deleted (the delete is chunked).\n"
            "  Both systems still hold the run_id mapping, so re-running this\n"
            "  command is safe and will finish the job.\n"
        )
        return 1

    try:
        _delete_supabase(sb, all_run_ids, all_client_ids)
    except Exception as exc:
        print(
            f"\nFAILED during the Supabase delete: {exc}\n"
            "  OUT OF SYNC: BigQuery rows for these runs were DELETED, but Supabase\n"
            "  still holds them. The UI will show runs whose approved rows are gone\n"
            "  from the Truth Layer.\n"
            "  Supabase still holds the run_id mapping, so re-running this command\n"
            "  is safe and will finish the job.\n"
        )
        return 1

    # Re-count both systems rather than trusting the delete calls, so the
    # summary reports what is actually in each store now.
    leftover_sb_runs = leftover_sb_maps = 0
    for tenant, tenant_id in tenants:
        clients = _resolve_clients(sb, tenant, tenant_id, args.client, strict=strict)
        if args.client and not clients:
            continue
        r_runs, r_maps = _supabase_counts(sb, clients)
        leftover_sb_runs += sum(len(v) for v in r_runs.values())
        leftover_sb_maps += sum(r_maps.values())

    leftover_related = _related_counts(sb, all_run_ids)
    leftover_rules = _rule_counts(sb, all_client_ids)
    try:
        leftover_bq_runs, leftover_bq_maps = _bigquery_counts(bq, bqmod, all_run_ids)
        leftover_bq_revocations = _bigquery_revocation_count(bq, bqmod, all_run_ids)
        bq_verified = True
    except BigQueryUnavailable as exc:
        leftover_bq_runs = leftover_bq_maps = leftover_bq_revocations = -1
        bq_verified = False
        print(f"\n! Could not re-verify BigQuery: {exc}")

    print("\nReset complete.")
    print(f"  tenants          : {', '.join(n for n, _ in tenants)}")
    print(f"  clients reset    : {len(rows)} ({', '.join(r['label'] for r in rows)})")
    print(f"  runs targeted    : {len(all_run_ids)}")
    print(f"  BigQuery deleted : {total_bq} mapping row(s), {bq_revocations} revocation(s)")
    print(f"  Supabase deleted : {total_sb} mapping row(s), {related['exports']} export(s), "
          f"{related['export_mappings']} membership row(s), "
          f"{related['status_history']} history row(s)")
    print(f"  Cache rules      : {rules['mapping_rules']} rule(s), "
          f"{rules['mapping_rule_events']} rule event(s) deleted")

    out_of_sync = []
    if leftover_sb_runs or leftover_sb_maps:
        out_of_sync.append(f"SUPABASE still has {leftover_sb_runs} run(s), {leftover_sb_maps} mapping row(s)")
    if any(leftover_rules.values()):
        out_of_sync.append(
            f"SUPABASE cache tables still have {leftover_rules['mapping_rules']} rule(s), "
            f"{leftover_rules['mapping_rule_events']} rule event(s)"
        )
    if any(leftover_related.values()):
        out_of_sync.append(
            f"SUPABASE lifecycle tables still have {leftover_related['exports']} export(s), "
            f"{leftover_related['export_mappings']} membership row(s), "
            f"{leftover_related['status_history']} history row(s)"
        )
    if bq_verified and (leftover_bq_runs or leftover_bq_maps or leftover_bq_revocations):
        out_of_sync.append(
            f"BIGQUERY still has {leftover_bq_runs} run(s), {leftover_bq_maps} mapping row(s), "
            f"{leftover_bq_revocations} revocation(s)"
        )
    if not bq_verified:
        out_of_sync.append("BIGQUERY could not be re-verified — state unknown")

    if out_of_sync:
        print("\n  WARNING — post-delete verification failed:")
        for line in out_of_sync:
            print(f"    - {line}")
        print()
        return 1

    print("  verified         : both BigQuery and Supabase are clean\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
