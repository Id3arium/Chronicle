"""state.json read/write + freshness derivation.

Shape:
{
  "last_ingest": "2026-04-22T14:00:00Z",
  "conversations": {
    "<uuid>": {
      "title": str,
      "created_at": iso,
      "updated_at": iso,
      "project_name": str | null,
      "conversation_file": "conversations/YYYY-MM/{uuid}.json",
      "summary_file": "summaries/YYYY-MM/{uuid}.md" | null,
      "summarized_at": iso | null,
      "deleted_at": iso | null,
      "first_seen": iso
    }
  },
  "entries": {
    "<period_label>": {
      "entry_file": "entries/<period_label>_Entry.md",
      "synthesized_at": iso,
      "range_start": "YYYY-MM-DD",
      "range_end": "YYYY-MM-DD"
    }
  },
  "processed_exports": ["<export filename>", ...]
}

Staleness is derived, never stored:
- summary_stale(uuid): no summarized_at OR updated_at > summarized_at
- entry_stale(period): no entry yet OR any non-deleted conversation in the
  period's date range has updated_at > entry.synthesized_at
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import state_file


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load() -> dict[str, Any]:
    path = state_file()
    if not path.exists():
        return {
            "last_ingest": None,
            "conversations": {},
            "entries": {},
            "processed_exports": [],
        }
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    # Backfill missing top-level keys so older state files keep working.
    data.setdefault("conversations", {})
    data.setdefault("entries", {})
    data.setdefault("processed_exports", [])
    return data


def save(state: dict[str, Any]) -> None:
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")
    tmp.replace(path)


# ────────────────── freshness ──────────────────

def summary_stale(conv: dict[str, Any]) -> bool:
    if conv.get("deleted_at"):
        return False
    if not conv.get("summarized_at"):
        return True
    return conv["updated_at"] > conv["summarized_at"]


def stale_summary_uuids(state: dict[str, Any]) -> list[str]:
    return [uuid for uuid, c in state["conversations"].items() if summary_stale(c)]


def conversations_in_period(
    state: dict[str, Any], range_start: str, range_end: str
) -> list[tuple[str, dict[str, Any]]]:
    """UUIDs whose created_at falls in [range_start, range_end] (YYYY-MM-DD).
    Excludes soft-deleted entries.
    """
    start = f"{range_start}T00:00:00Z"
    end = f"{range_end}T23:59:59Z"
    out = []
    for uuid, c in state["conversations"].items():
        if c.get("deleted_at"):
            continue
        created = c.get("created_at") or ""
        if start <= created <= end:
            out.append((uuid, c))
    return out


def entry_stale(
    state: dict[str, Any], period_label: str, range_start: str, range_end: str
) -> bool:
    """Entry is stale iff:
    - it doesn't exist yet, OR
    - any conversation in its range has updated_at > synthesized_at, OR
    - any child-tier entry it depends on is itself stale or younger than it.
    The third clause handles rollup cascade: a quarterly is stale if any
    monthly it's built from has been re-synthesized since the quarterly ran.
    """
    entry = state["entries"].get(period_label)
    if not entry or not entry.get("synthesized_at"):
        return True
    threshold = entry["synthesized_at"]
    for _, c in conversations_in_period(state, range_start, range_end):
        if c["updated_at"] > threshold:
            return True
    # Cascade check: if a child entry was re-synthesized after this one, we're stale.
    for child_label in entry.get("children", []):
        child = state["entries"].get(child_label)
        if child and child.get("synthesized_at", "") > threshold:
            return True
    return False
