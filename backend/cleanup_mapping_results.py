"""
One-time cleanup: remove duplicate rows from mapping_results.

A duplicate is any (run_id, source_column) pair with more than one row.
We keep the row with the lexicographically smallest UUID (arbitrary but
deterministic) and delete the rest.

Run from the backend/ directory:
  python cleanup_mapping_results.py [--dry-run]
"""
import os
import sys
import logging
from collections import defaultdict

from dotenv import load_dotenv
from supabase import create_client

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DRY_RUN = "--dry-run" in sys.argv


def paginate_all(sb, table: str, select: str, page_size: int = 1000) -> list[dict]:
    """Fetch all rows from a table using range-based pagination."""
    rows = []
    offset = 0
    while True:
        resp = (
            sb.table(table)
            .select(select)
            .range(offset, offset + page_size - 1)
            .execute()
        )
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def main():
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])

    logger.info("Fetching all mapping_results rows…")
    all_rows = paginate_all(sb, "mapping_results", "id, run_id, source_column")
    logger.info("Total rows fetched: %d", len(all_rows))

    # Group row ids by (run_id, source_column)
    groups: dict[tuple, list[str]] = defaultdict(list)
    for row in all_rows:
        key = (row["run_id"], row["source_column"])
        groups[key].append(row["id"])

    # Collect ids to delete: everything except the min-uuid per group
    to_delete: list[str] = []
    for (run_id, src_col), ids in groups.items():
        if len(ids) > 1:
            ids_sorted = sorted(ids)   # keep smallest UUID lexicographically
            to_delete.extend(ids_sorted[1:])
            logger.info(
                "  duplicate: run=%s col=%-40s  keeping %s, deleting %d",
                run_id[:8], src_col, ids_sorted[0][:8], len(ids_sorted) - 1,
            )

    if not to_delete:
        logger.info("No duplicates found — nothing to delete.")
        return

    logger.info(
        "%s %d duplicate row(s) across %d (run_id, source_column) group(s).",
        "Would delete" if DRY_RUN else "Deleting",
        len(to_delete),
        sum(1 for ids in groups.values() if len(ids) > 1),
    )

    if DRY_RUN:
        logger.info("Dry-run mode — no changes made.")
        return

    # Delete in batches of 100
    CHUNK = 100
    deleted = 0
    for i in range(0, len(to_delete), CHUNK):
        batch = to_delete[i : i + CHUNK]
        sb.table("mapping_results").delete().in_("id", batch).execute()
        deleted += len(batch)
        logger.info("  deleted %d / %d", deleted, len(to_delete))

    logger.info("Done. Removed %d duplicate row(s).", deleted)


if __name__ == "__main__":
    main()
