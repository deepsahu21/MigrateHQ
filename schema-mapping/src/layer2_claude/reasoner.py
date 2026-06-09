import os
import json
import time
import re
from pathlib import Path
from dotenv import load_dotenv
from google import genai

load_dotenv()

client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

# Load WMS domain context once at import time
_CONTEXT_PATH = Path(__file__).parent.parent.parent / "claude.md"
CLAUDE_CONTEXT: str = _CONTEXT_PATH.read_text() if _CONTEXT_PATH.exists() else ""


def semantic_match_batch(
    batch_name: str,
    source_columns: list[str],
    target_columns: list[str],
    source_samples: dict[str, list],
    target_samples: dict[str, list],
) -> list[dict]:
    """
    Match a batch of semantically-related source columns to target candidates in one LLM call.

    Returns list of {"source", "target", "confidence", "reasoning"} dicts.
    """
    src_block = "\n".join(
        f"  - {col}: {source_samples.get(col, [])}" for col in source_columns
    )
    tgt_block = "\n".join(
        f"  - {col}: {target_samples.get(col, [])}" for col in target_columns
    )

    prompt = f"""{CLAUDE_CONTEXT}

---

## Task
You are matching SOURCE columns to TARGET columns for a WMS database migration.
These source columns are semantically related — reason about them TOGETHER as a group.
They compete for the target columns listed; assign each source to exactly one target (no two sources may claim the same target).

SOURCE columns (with sample values):
{src_block}

TARGET candidates (with sample values):
{tgt_block}

Apply the mutual-exclusion constraint: if two source columns are both date fields competing for two target date fields, determine which pairing makes more semantic sense (e.g., carrier handoff → shipped_date, customer receipt → received_date).

Return ONLY a JSON array — no markdown, no extra text:
[
  {{"source": "col_name", "target": "col_name_or_null", "confidence": 0.0-1.0, "reasoning": "one sentence"}},
  ...
]
Every source column must appear exactly once in the output."""

    print(f"\n=== BATCH: {batch_name} ===")
    print(f"Source columns: {source_columns}")
    print(f"Target candidates: {target_columns}")
    print("Prompt being sent to Gemini:")
    print(prompt)
    print("---")

    raw = ""
    for attempt in range(4):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
            )
            raw = response.text.strip()

            # Token counting via usage_metadata when available
            usage = getattr(response, "usage_metadata", None)
            if usage:
                tokens_in = getattr(usage, "prompt_token_count", "?")
                tokens_out = getattr(usage, "candidates_token_count", "?")
            else:
                tokens_in = round(len(prompt) / 4)
                tokens_out = round(len(raw) / 4)

            break
        except Exception as e:
            err_str = str(e)
            # Respect the suggested retry delay from 429 responses
            retry_match = re.search(r"retry.*?(\d+(?:\.\d+)?)s", err_str, re.IGNORECASE)
            wait = float(retry_match.group(1)) + 2 if retry_match else (10 * (attempt + 1))
            if attempt < 3:
                print(f"  [WARN] attempt {attempt+1} failed ({err_str[:80]}...), retrying in {wait:.0f}s")
                time.sleep(wait)
            else:
                print(f"Gemini response: ERROR — {err_str[:200]}")
                print("Estimated tokens in/out: ?/?")
                return [
                    {"source": s, "target": None, "confidence": 0.0, "reasoning": f"API error: {e}"}
                    for s in source_columns
                ]

    print("Gemini response:")
    print(raw)
    print(f"Estimated tokens in/out: {tokens_in}/{tokens_out}")

    # Strip markdown fences if present
    clean = raw
    if clean.startswith("```"):
        parts = clean.split("```")
        clean = parts[1] if len(parts) > 1 else clean
        if clean.startswith("json"):
            clean = clean[4:]
    clean = clean.strip()

    try:
        matches = json.loads(clean)
    except json.JSONDecodeError:
        print(f"  [WARN] Could not parse JSON: {clean[:200]}")
        matches = [
            {"source": s, "target": None, "confidence": 0.0, "reasoning": "JSON parse error"}
            for s in source_columns
        ]

    print("Matches extracted:")
    for m in matches:
        print(f"  {m.get('source')} → {m.get('target')} | {m.get('confidence', 0.0):.2f}")
    print()

    return matches


# ── Legacy single-column function kept for backwards compat ───────────────────

def reason_best_match(
    source_col: str,
    target_candidates: list,
    source_samples: list,
    target_samples_map: dict,
) -> dict:
    """Single-column wrapper around semantic_match_batch for orchestrator compatibility."""
    results = semantic_match_batch(
        batch_name=source_col,
        source_columns=[source_col],
        target_columns=target_candidates,
        source_samples={source_col: source_samples},
        target_samples=target_samples_map,
    )
    if results:
        r = results[0]
        return {"best_match": r.get("target"), "confidence": r.get("confidence", 0.0), "reasoning": r.get("reasoning", "")}
    return {"best_match": None, "confidence": 0.0, "reasoning": "no result"}


if __name__ == "__main__":
    results = semantic_match_batch(
        batch_name="smoke-test",
        source_columns=["order_id", "customer_id"],
        target_columns=["transaction_ref", "buyer_key"],
        source_samples={
            "order_id": ["e481f51cbdc54678b7cc49136f2d6af7", "53cdb2fc8bc7dce0b6741e2150273451"],
            "customer_id": ["9ef432eb6251297304e76186b10a928d", "b0830fb4747a6c6d20dea0b8c802d7ef"],
        },
        target_samples={
            "transaction_ref": ["e481f51cbdc54678b7cc49136f2d6af7", "53cdb2fc8bc7dce0b6741e2150273451"],
            "buyer_key": ["9ef432eb6251297304e76186b10a928d", "b0830fb4747a6c6d20dea0b8c802d7ef"],
        },
    )
    print(results)
