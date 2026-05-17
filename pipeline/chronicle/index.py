"""`chronicle index` — build/rebuild the search index.

The index is a single JSON file at data/index.json that contains:
1. Per-conversation metadata entries (for display in search results)
2. An inverted index mapping individual search terms → uuids with weights

Keywords get weight 3 (highest confidence — explicitly tagged for search).
Topic tokens get weight 2 (conceptual "what it's about").
Title tokens get weight 1 (lowest — incidental matches).

`chronicle find` reads this file for fast, weighted search.
The index is rebuilt automatically after each successful summarize run.
Manual rebuild: `chronicle index`.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import state as state_mod
from .metrics import parse_frontmatter
from .paths import data_root


_STOP_WORDS = frozenset({
    "a", "an", "and", "as", "at", "be", "by", "for", "from", "has", "he",
    "in", "is", "it", "its", "of", "on", "or", "she", "that", "the", "to",
    "was", "were", "will", "with", "vs", "etc",
})


def _tokenize(text: str) -> list[str]:
    """Split text into lowercase tokens, dropping stop words and short junk."""
    tokens = re.findall(r"[a-z0-9][a-z0-9._/'-]*[a-z0-9]|[a-z0-9]", text.lower())
    return [t for t in tokens if t not in _STOP_WORDS and len(t) > 1]


def _add_to_inverted(
    inverted: dict[str, list[dict[str, Any]]],
    tokens: list[str],
    uuid: str,
    weight: int,
) -> None:
    """Add token→uuid mappings at the given weight. Deduplicates per uuid."""
    for token in tokens:
        if token not in inverted:
            inverted[token] = []
        # Don't add duplicate uuid entries for the same token; keep highest weight.
        existing = next((e for e in inverted[token] if e["uuid"] == uuid), None)
        if existing:
            existing["weight"] = max(existing["weight"], weight)
        else:
            inverted[token].append({"uuid": uuid, "weight": weight})


def index_path():
    from pathlib import Path
    return data_root() / "index.json"


def build_index(state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the search index from state + summary frontmatter. Returns the
    index dict and writes it to data/index.json."""
    if state is None:
        state = state_mod.load()

    entries: list[dict[str, Any]] = []
    inverted: dict[str, list[dict[str, Any]]] = {}

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

        title = c.get("title") or ""
        keywords = fm.get("keywords") or fm.get("tags") or ""
        topics = fm.get("topics") or ""

        entries.append({
            "uuid": uuid,
            "title": title,
            "created_at": (c.get("created_at") or "")[:10],
            "updated_at": c.get("updated_at") or "",
            "project": c.get("project_name") or fm.get("project") or "",
            "categories": fm.get("categories") or "",
            "topics": topics,
            "keywords": keywords,
            "significance": c.get("significance") or fm.get("significance") or "",
            "summary_file": sf or "",
            "original_words": c.get("original_words") or 0,
            "summary_words": c.get("summary_words") or 0,
            "has_summary": bool(sf and c.get("summarized_at")),
        })

        # Build inverted index: keywords (weight 3), topics (weight 2), title (weight 1)
        kw_tokens = _tokenize(keywords)
        topic_tokens = _tokenize(topics)
        title_tokens = _tokenize(title)

        # Also index full multi-word keyword/topic phrases as joined tokens
        # so "michael levin" is findable as "michael" and "levin" individually.
        _add_to_inverted(inverted, kw_tokens, uuid, weight=3)
        _add_to_inverted(inverted, topic_tokens, uuid, weight=2)
        _add_to_inverted(inverted, title_tokens, uuid, weight=1)

    # Compute IDF scores: log(N / df) for each term.
    import math
    n_docs = len(entries) or 1
    idf: dict[str, float] = {}
    for term, hits in inverted.items():
        df = len(set(h["uuid"] for h in hits))
        idf[term] = round(math.log(n_docs / df), 3)

    idx = {
        "built_at": state_mod.now_iso(),
        "conversation_count": len(entries),
        "entries": entries,
        "inverted": inverted,
        "idf": idf,
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
