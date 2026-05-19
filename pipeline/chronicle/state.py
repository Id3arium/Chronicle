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
  "processed_imports": ["<export filename>", ...]
}

Staleness is derived, never stored:
- summary_stale(uuid): no summarized_at OR updated_at > summarized_at
- entry_stale(period): no entry yet OR any non-deleted conversation in the
  period's date range has updated_at > entry.synthesized_at
"""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .paths import state_file


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lock_path() -> Path:
    """Dedicated lockfile next to state.json. Using a separate file (not
    state.json itself) because save() does atomic replace via tmp.replace(),
    which would invalidate any flock held on the original fd."""
    return state_file().with_suffix(".lock")


@contextmanager
def _shared_lock() -> Iterator[None]:
    """Shared (read) lock — multiple readers can hold simultaneously."""
    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch(exist_ok=True)
    fd = lock.open("r")
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


@contextmanager
def _exclusive_lock() -> Iterator[None]:
    """Exclusive (write) lock — blocks readers and other writers."""
    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch(exist_ok=True)
    fd = lock.open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def _backfill(data: dict[str, Any]) -> dict[str, Any]:
    """Backfill missing top-level keys so older state files keep working."""
    data.setdefault("conversations", {})
    data.setdefault("entries", {})
    data.setdefault("processed_imports", [])
    # Migrate legacy key if present.
    if "processed_exports" in data and "processed_imports" not in data:
        data["processed_imports"] = data["processed_exports"]
    data.setdefault("processed_exports", [])  # keep for backward compat
    return data


def load() -> dict[str, Any]:
    path = state_file()
    if not path.exists():
        return _backfill({
            "last_ingest": None,
            "conversations": {},
            "entries": {},
            "processed_imports": [],
        })
    with _shared_lock():
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    return _backfill(data)


def save(state: dict[str, Any]) -> None:
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _exclusive_lock():
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.write("\n")
        tmp.replace(path)


def merge_save(updates: dict[str, Any]) -> None:
    """Atomic read-modify-write: re-read state from disk, merge in the
    provided updates, save. Use this instead of save() when a long-running
    process (synthesize, summarize) needs to write its results without
    clobbering changes from other processes that ran concurrently.

    `updates` is a dict of top-level keys to merge:
      - "entries": {label: record} — merged into state["entries"]
      - "conversations": {uuid: record} — merged into state["conversations"]
      - "last_ingest": str — overwrites
      - "processed_imports": [name, ...] — unioned
    """
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _exclusive_lock():
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                state = json.load(f)
            state = _backfill(state)
        else:
            state = _backfill({
                "last_ingest": None,
                "conversations": {},
                "entries": {},
                "processed_imports": [],
            })

        # Merge each key type appropriately.
        for key, val in updates.items():
            if key in ("conversations", "entries") and isinstance(val, dict):
                state.setdefault(key, {}).update(val)
            elif key in ("processed_imports", "processed_exports") and isinstance(val, list):
                existing = set(state.get(key, []))
                existing.update(val)
                state[key] = sorted(existing)
            else:
                state[key] = val

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


def reconcile_summaries(state: dict[str, Any]) -> list[str]:
    """Sync state with disk for every summarized conversation:

    - If the summary .md is missing: drop the freshness marker so the
      conversation falls back into the stale list (allows "force re-run by
      deleting the file").
    - If the summary .md exists but `significance` isn't in state yet: read it
      from the frontmatter and backfill. Also backfills any other frontmatter
      fields we track (future-proofing).

    Returns the list of UUIDs that were changed. Caller should `save(state)`
    if the list is non-empty.
    """
    from .metrics import parse_frontmatter
    from .paths import data_root
    root = data_root()
    changed = []
    for uuid, c in state["conversations"].items():
        if c.get("deleted_at") or not c.get("summarized_at"):
            continue
        sf = c.get("summary_file")
        if not sf:
            continue
        path = root / sf
        if not path.exists():
            # File is gone — drop the freshness marker so it's stale again.
            for k in (
                "summarized_at",
                "summary_chars",
                "summary_words",
                "summary_tokens_est",
                "compression_ratio",
                "significance",
            ):
                c.pop(k, None)
            changed.append(uuid)
            continue
        # File exists — backfill significance, summarized_at, and anything
        # else from frontmatter that we care about but don't yet have in state.
        needs_backfill = (
            c.get("significance") is None
            or c.get("summarized_at") is None
        )
        if needs_backfill:
            try:
                fm = parse_frontmatter(path.read_text(encoding="utf-8"))
                did_change = False
                if c.get("significance") is None and fm.get("significance"):
                    c["significance"] = fm["significance"]
                    did_change = True
                if c.get("summarized_at") is None and fm.get("summarized_at"):
                    c["summarized_at"] = fm["summarized_at"]
                    did_change = True
                if did_change:
                    changed.append(uuid)
            except OSError:
                pass
    return changed


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
