#!/usr/bin/env python3
"""
Dev/demo-only SNAPSHOT of a single client's mapping run.

Captures one run's mapping_runs row, all of its mapping_results rows, and the
persisted source file(s) into demo/snapshots/, so a demo can later be restored
to a known baseline. Read-only against Supabase — this script never writes to
any database.

    python scripts/snapshot_demo_data.py --tenant tenant_2 --client clientA \
        --run-id f8dce22e-925e-4d90-826b-55b6501bbe3f

WHY original_status IS RECOMPUTED RATHER THAN COPIED
----------------------------------------------------
A row's CURRENT `status` reflects human review — approve / reject / revoke.
A demo reset needs the status the PIPELINE assigned at ingest time, before any
human touched it. That value is not stored anywhere, but it is deterministic:
it is a pure function of (target_column, layer, confidence), none of which any
endpoint ever overwrites for a live row (see build_sesh_prompt_outputs/
prompt44.txt §1 — /approve writes target_column only on a human correction, of
which there are currently zero; /reject and /revoke touch `status` alone).

So `original_status` is RECOMPUTED by re-running the pipeline's own approval
gate over each row's untouched fields. It is never read off `status`. On a run
where nobody has reviewed anything the two are necessarily identical — that is
the gate agreeing with itself, not the script failing to do work. `status` is
also recorded verbatim, so the two are always comparable.

THE GATE IS IMPORTED, NOT REIMPLEMENTED
---------------------------------------
`_is_approved()` is imported from schema-mapping/bigquery_loader.py — the very
function the pipeline calls. A local copy would be a second source of truth
that drifts silently the moment the threshold or a special case changes, and
the failure mode of that drift is a snapshot that looks fine and restores a
wrong review queue. An unavailable import is a hard error here, deliberately;
there is no fallback copy.

The gate has two special cases that a naive `confidence >= 0.75` misses:

  * a row with NO TARGET is never approved, whatever its confidence — this is
    how F1 collision tie-escalation reaches the review queue despite
    deliberately preserving a high score;
  * layer='cache' is ALWAYS approved regardless of confidence, because the
    stored score is the model's from when a human confirmed it and is often
    below threshold.

TENANT IS REQUIRED, AND AMBIGUITY IS FATAL
------------------------------------------
`clientA` and `clientB` each exist under BOTH tenant_1 and tenant_2 as
genuinely separate client rows with separate runs. `--tenant` therefore has no
default and resolution refuses to guess: an ambiguous or missing
(tenant, client) pair exits with the candidates listed rather than silently
taking the first match.
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional

from dotenv import load_dotenv
from supabase import create_client, Client

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_UPLOADS_DIR = os.path.join(_REPO_ROOT, "schema-mapping", "data", "uploads")
_SNAPSHOT_DIR = os.path.join(_REPO_ROOT, "demo", "snapshots")

# Where the approval gate actually lives, recorded into every snapshot so a
# reader can audit the reconstruction against the code that produced it.
_GATE_SOURCE = (
    "schema-mapping/bigquery_loader.py:425 "
    "(status assignment) via _is_approved() at "
    "schema-mapping/bigquery_loader.py:51-80, APPROVAL_THRESHOLD=0.75 at line 48"
)

sys.path.insert(0, os.path.join(_REPO_ROOT, "schema-mapping"))
try:
    from bigquery_loader import _is_approved, APPROVAL_THRESHOLD  # noqa: E402
except Exception as exc:  # pragma: no cover - environment problem, not logic
    sys.exit(
        "error: could not import the approval gate from "
        "schema-mapping/bigquery_loader.py:\n"
        "       {}\n"
        "       Refusing to continue. Reimplementing the gate locally would create a\n"
        "       second source of truth that drifts from the pipeline silently.".format(exc)
    )


# -- connections --------------------------------------------------------------

def _connect_supabase() -> Client:
    """Reuse the backend's credentials; fall back to the pipeline's .env.

    Same construction as scripts/reset_demo_data.py, deliberately — the two
    scripts are a matched pair and should read the same database the same way.
    """
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


# -- resolution (read-only, refuses to guess) ---------------------------------

def _resolve_tenant(sb: Client, name: str) -> str:
    rows = sb.table("tenants").select("id, name").eq("name", name).execute().data or []
    if not rows:
        known = sb.table("tenants").select("name").execute().data or []
        sys.exit(
            "error: tenant '{}' not found.\n"
            "       Known tenants: {}".format(
                name, ", ".join(sorted(t["name"] for t in known)) or "(none)"
            )
        )
    if len(rows) > 1:
        sys.exit("error: tenant name '{}' is ambiguous ({} rows).".format(name, len(rows)))
    return rows[0]["id"]


def _resolve_client(sb: Client, tenant: str, tenant_id: str, client: str) -> dict:
    """Exactly one client, or exit.

    The (tenant_id, source_dataset) pair is the real key — schema.sql makes it
    unique, and a bare name lookup would cross tenants. clientA and clientB each
    exist under two tenants, so this is not a hypothetical.
    """
    rows = (
        sb.table("clients")
        .select("id, source_dataset, display_name, tenant_id")
        .eq("tenant_id", tenant_id)
        .eq("source_dataset", client)
        .execute()
        .data
    ) or []

    if not rows:
        siblings = (
            sb.table("clients").select("source_dataset").eq("tenant_id", tenant_id).execute().data
        ) or []
        elsewhere = (
            sb.table("clients").select("tenant_id").eq("source_dataset", client).execute().data
        ) or []
        msg = [
            "error: no client '{}' under tenant '{}'.".format(client, tenant),
            "       Clients in this tenant: {}".format(
                ", ".join(sorted(c.get("source_dataset") or "?" for c in siblings)) or "(none)"
            ),
        ]
        if elsewhere:
            msg.append(
                "       NOTE: a client named '{}' does exist under {} other tenant(s). "
                "This script will not reach across tenants — pass the right --tenant.".format(
                    client, len(elsewhere)
                )
            )
        sys.exit("\n".join(msg))

    if len(rows) > 1:
        sys.exit(
            "error: (tenant '{}', client '{}') is AMBIGUOUS — {} client rows match:\n{}\n"
            "       Refusing to pick one. Resolve the duplicate in the clients table.".format(
                tenant, client, len(rows),
                "\n".join("         client_id={}".format(r["id"]) for r in rows),
            )
        )
    return rows[0]


def _resolve_run(sb: Client, client_id: str, label: str, run_id: Optional[str]) -> dict:
    """The one run to snapshot, or exit with the candidates listed.

    Never combines runs and never guesses when several exist: the choice of
    baseline is a judgement call about what the demo should show, not something
    a script can infer from timestamps.
    """
    runs = sb.table("mapping_runs").select("*").eq("client_id", client_id).execute().data or []
    if not runs:
        sys.exit("error: client {} has no mapping_runs rows — nothing to snapshot.".format(label))

    if run_id is not None:
        for r in runs:
            if r["run_id"] == run_id:
                return r
        sys.exit(
            "error: run_id {} does not belong to {}.\n"
            "       That client's runs are:\n{}".format(
                run_id, label,
                "\n".join("         {}  {}".format(r["run_id"], r.get("created_at")) for r in runs),
            )
        )

    if len(runs) > 1:
        listing = []
        for r in sorted(runs, key=lambda x: x.get("created_at") or ""):
            n = (
                sb.table("mapping_results")
                .select("id", count="exact")
                .eq("run_id", r["run_id"])
                .execute()
                .count
            ) or 0
            listing.append(
                "         {}  {}  rows={}".format(r["run_id"], r.get("created_at"), n)
            )
        sys.exit(
            "error: {} has {} runs. Pass --run-id to choose the demo baseline.\n{}".format(
                label, len(runs), "\n".join(listing)
            )
        )
    return runs[0]


# -- reconstruction -----------------------------------------------------------

def _original_status(row: dict) -> str:
    """The status the pipeline WOULD have assigned to this row at ingest time.

    Re-runs the pipeline's own gate over the row's untouched fields. The dict
    keys are translated because the gate reads the orchestrator's in-memory
    shape ('target') while the table stores 'target_column'; nothing else about
    the decision differs.
    """
    info = {
        "target": row.get("target_column"),
        "layer": row.get("layer"),
        "confidence": row.get("confidence"),
    }
    return "approved" if _is_approved(info) else "pending_review"


# -- source files -------------------------------------------------------------

def _copy_source_files(run_id: str, client: str) -> Dict[str, object]:
    """Copy the WHOLE upload directory for the run.

    Not one file: a PDF source persists as BOTH source.pdf (the original the
    signed-URL flow serves) and source_extracted.csv (the grid pdfplumber
    pulled out, which is what export actually reads). Copying only one of them
    would produce a snapshot that restores a run the UI or the export path
    cannot use.

    Missing files are reported, never fatal — the source file matters for the
    UI's Source column and the signed-URL flow, not for the reset itself.
    """
    src_dir = os.path.join(_UPLOADS_DIR, run_id)
    dest_dir = os.path.join(_SNAPSHOT_DIR, "{}_source".format(client))

    if not os.path.isdir(src_dir):
        return {
            "present": False,
            "note": "no upload directory at schema-mapping/data/uploads/{} — "
                    "source file(s) unavailable".format(run_id),
            "stored_dir": os.path.relpath(dest_dir, _REPO_ROOT),
            "files": [],
        }

    names = sorted(n for n in os.listdir(src_dir) if os.path.isfile(os.path.join(src_dir, n)))
    if not names:
        return {
            "present": False,
            "note": "upload directory exists but is empty",
            "stored_dir": os.path.relpath(dest_dir, _REPO_ROOT),
            "files": [],
        }

    os.makedirs(dest_dir, exist_ok=True)
    files: List[dict] = []
    for name in names:
        src_path = os.path.join(src_dir, name)
        shutil.copy2(src_path, os.path.join(dest_dir, name))
        files.append({
            "original_filename": name,
            "stored_path": os.path.relpath(os.path.join(dest_dir, name), _REPO_ROOT),
            "bytes": os.path.getsize(src_path),
        })

    return {
        "present": True,
        "stored_dir": os.path.relpath(dest_dir, _REPO_ROOT),
        "files": files,
    }


# -- main ---------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Snapshot one client's mapping run for demo reset (dev only, read-only).",
    )
    parser.add_argument(
        "--tenant", required=True, metavar="NAME",
        help="Tenant name. REQUIRED and has no default: clientA and clientB each "
             "exist under more than one tenant, so a default would silently pick "
             "the wrong client's data.",
    )
    parser.add_argument(
        "--client", required=True, metavar="NAME",
        help="Client source_dataset (e.g. clientA), resolved within --tenant.",
    )
    parser.add_argument(
        "--run-id", metavar="UUID",
        help="Run to snapshot. Optional only when the client has exactly one run; "
             "otherwise the script lists the candidates and exits rather than guessing.",
    )
    args = parser.parse_args()

    sb = _connect_supabase()

    tenant_id = _resolve_tenant(sb, args.tenant)
    client_row = _resolve_client(sb, args.tenant, tenant_id, args.client)
    client_id = client_row["id"]
    label = "{}/{}".format(args.tenant, args.client)

    run_row = _resolve_run(sb, client_id, label, args.run_id)
    run_id = run_row["run_id"]

    results = (
        sb.table("mapping_results").select("*").eq("run_id", run_id).execute().data
    ) or []
    if not results:
        sys.exit("error: run {} has no mapping_results rows — refusing to write an "
                 "empty snapshot.".format(run_id))

    # Stable order so re-running produces a byte-comparable file.
    results.sort(key=lambda r: r.get("source_column") or "")

    snapshot_results = []
    differs = 0
    for row in results:
        original = _original_status(row)
        if original != row.get("status"):
            differs += 1
        entry = {k: v for k, v in row.items() if k not in ("id", "run_id")}
        entry["original_status"] = original
        snapshot_results.append(entry)

    source_file = _copy_source_files(run_id, args.client)

    snapshot = {
        "client": args.client,
        "tenant": args.tenant,
        "client_id": client_id,
        "source_run_id": run_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "gate_logic_source": _GATE_SOURCE,
        "approval_threshold": APPROVAL_THRESHOLD,
        "source_file": source_file,
        # Verbatim minus run_id, per spec. NOTE for whoever restores this: the
        # row still carries `id` (its Supabase PK) and `client_id`. Both are
        # environment-specific — drop `id` and re-resolve `client_id` from
        # (tenant, client) on restore rather than trusting these.
        "mapping_runs": {k: v for k, v in run_row.items() if k != "run_id"},
        "mapping_results": snapshot_results,
    }

    os.makedirs(_SNAPSHOT_DIR, exist_ok=True)
    out_path = os.path.join(_SNAPSHOT_DIR, "{}.json".format(args.client))
    with open(out_path, "w") as fh:
        json.dump(snapshot, fh, indent=2, sort_keys=False, default=str)
        fh.write("\n")

    current = {}
    original = {}
    for row in snapshot_results:
        current[row.get("status")] = current.get(row.get("status"), 0) + 1
        original[row["original_status"]] = original.get(row["original_status"], 0) + 1

    print("\nSnapshot written: {}".format(os.path.relpath(out_path, _REPO_ROOT)))
    print("  tenant / client  : {}  (client_id {})".format(label, client_id))
    print("  run_id           : {}".format(run_id))
    print("  mapping_results  : {} row(s)".format(len(snapshot_results)))
    print("  current status   : {}".format(current))
    print("  original_status  : {}   (recomputed via the pipeline gate)".format(original))
    print("  differing rows   : {}{}".format(
        differs,
        "  (expected when nobody has reviewed this run)" if differs == 0 else "",
    ))
    if source_file["present"]:
        print("  source file(s)   : {} -> {}".format(
            ", ".join(f["original_filename"] for f in source_file["files"]),
            source_file["stored_dir"],
        ))
    else:
        print("  source file(s)   : MISSING — {}".format(source_file["note"]))
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
