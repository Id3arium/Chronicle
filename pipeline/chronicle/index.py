"""`chronicle index` — build/rebuild the search index.

The index is a single JSON file at data/index.json that contains all
searchable metadata for every summarized conversation. `chronicle find`
reads this file instead of opening every summary individually.

The index is rebuilt automatically after each successful summarize run.
Manual rebuild: `chronicle index`.
"""

from __future__ import annotations

import json
from typing import Any

from . import state as state_mod
from .metrics import parse_frontmatter
from .paths import data_root


def index_path():
    from pathlib import Path
    return data_root() / "index.json"


def build_index(state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the search index from state + summary frontmatter. Returns the
    index dict and writes it to data/index.json."""
    if state is None:
        state = state_mod.load()

    entries: list[dict[str, Any]] = []
    for uuid, c in state["conversations"].items():
        if c.get("deleted_at"):
            continue
        sf = c.get("summary_file")
        fm: dict[str, Any] = {}
        if sf:
            path = data_root() / sf
            if path.exists():
                try:
                    fm = parse_frontmatter(path.read_text(encoding="utf-8"))
                except OSError:
                    pass

        entries.append({
            "uuid": uuid,
            "title": c.get("title") or "",
            "created_at": (c.get("created_at") or "")[:10],
            "updated_at": c.get("updated_at") or "",
            "project": c.get("project_name") or fm.get("project") or "",
            "categories": fm.get("categories") or "",
            "topics": fm.get("topics") or "",
            "tags": fm.get("tags") or "",
            "significance": c.get("significance") or fm.get("significance") or "",
            "summary_file": sf or "",
            "original_words": c.get("original_words") or 0,
            "summary_words": c.get("summary_words") or 0,
            "has_summary": bool(sf and c.get("summarized_at")),
        })

    idx = {
        "built_at": state_mod.now_iso(),
        "conversation_count": len(entries),
        "entries": entries,
    }

    out = index_path()
    out.write_text(json.dumps(idx, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return idx


def load_index() -> dict[str, Any] | None:
    """Load the index from disk. Returns None if it doesn't exist."""
    p = index_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def run(args: Any) -> None:
    """CLI entry point for `chronicle index`."""
    state = state_mod.load()
    idx = build_index(state)
    print(f"Index built: {idx['conversation_count']} conversations → {index_path()}")
