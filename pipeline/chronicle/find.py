"""`chronicle find` — search conversations by keyword, tag, topic, or body text.

Searches frontmatter fields (title, topics, tags, categories) by default.
With --body, also searches summary prose. Results are ranked by number of
query terms matched, then by significance (high > medium > low).

Uses data/index.json for fast frontmatter-only searches (one file read
instead of N). Falls back to per-file reads if index is missing or --body
is specified.
"""

from __future__ import annotations

import re
from typing import Any

from . import state as state_mod
from .calendar import PeriodParseError, parse_period
from .index import load_index
from .metrics import parse_frontmatter
from .paths import data_root


SIG_ORDER = {"high": 0, "medium": 1, "med": 1, "low": 2}


def _normalize_sig(s: str | None) -> str:
    if not s:
        return "medium"
    return "medium" if s == "med" else s


def _search_indexed_entry(
    entry: dict[str, Any],
    terms: list[str],
) -> dict[str, Any] | None:
    """Search a single index entry (frontmatter only, no disk reads)."""
    searchable = " ".join([
        entry.get("title", ""),
        entry.get("topics", ""),
        entry.get("tags", ""),
        entry.get("categories", ""),
        entry.get("project", ""),
    ]).lower()

    matches = [t for t in terms if t in searchable]
    if not matches:
        return None

    return {
        "uuid": entry["uuid"],
        "title": entry.get("title") or "(untitled)",
        "created_at": entry.get("created_at", "")[:10],
        "significance": _normalize_sig(entry.get("significance")),
        "topics": entry.get("topics", ""),
        "tags": entry.get("tags", ""),
        "categories": entry.get("categories", ""),
        "matches": matches,
        "match_count": len(matches),
        "summary_file": entry.get("summary_file", ""),
    }


def _search_conversation(
    uuid: str,
    c: dict[str, Any],
    terms: list[str],
    search_body: bool,
) -> dict[str, Any] | None:
    """Search a single conversation's metadata + summary. Returns a result
    dict with match info, or None if no match. Reads summary from disk."""
    title = (c.get("title") or "").lower()
    sig = _normalize_sig(c.get("significance"))

    summary_text = ""
    fm: dict[str, Any] = {}
    sf = c.get("summary_file")
    if sf:
        path = data_root() / sf
        if path.exists():
            summary_text = path.read_text(encoding="utf-8")
            fm = parse_frontmatter(summary_text)

    topics = (fm.get("topics") or "").lower()
    tags = (fm.get("tags") or "").lower()
    categories = (fm.get("categories") or c.get("categories", "")).lower()
    project = (c.get("project_name") or fm.get("project") or "").lower()

    searchable = f"{title} {topics} {tags} {categories} {project}"

    if search_body and summary_text:
        body = summary_text
        if body.startswith("---"):
            end = body.find("\n---", 3)
            if end != -1:
                body = body[end + 4:]
        searchable += " " + body.lower()

    matches = [t for t in terms if t in searchable]
    if not matches:
        return None

    return {
        "uuid": uuid,
        "title": c.get("title") or "(untitled)",
        "created_at": (c.get("created_at") or "")[:10],
        "significance": sig,
        "topics": fm.get("topics") or "",
        "tags": fm.get("tags") or "",
        "categories": categories,
        "matches": matches,
        "match_count": len(matches),
        "summary_file": sf,
    }


def run(args: Any) -> None:
    terms = [t.lower() for t in args.query]
    search_body = getattr(args, "body", False)
    max_results = getattr(args, "limit", 20)
    sig_filter = getattr(args, "significance", None)
    if sig_filter:
        sig_filter = sig_filter.lower()
        if sig_filter == "med":
            sig_filter = "medium"

    # Fast path: use pre-built index for frontmatter-only searches.
    idx = load_index() if not search_body else None
    results = []

    if idx and not args.period:
        # Index-based search: one file read, no per-summary disk access.
        for entry in idx["entries"]:
            if sig_filter and _normalize_sig(entry.get("significance")) != sig_filter:
                continue
            hit = _search_indexed_entry(entry, terms)
            if hit:
                results.append(hit)
    else:
        # Slow path: read state + optionally summary bodies.
        state = state_mod.load()
        if args.period:
            try:
                _tier, rs, re_ = parse_period(args.period)
            except PeriodParseError as e:
                raise SystemExit(str(e))
            rows = state_mod.conversations_in_period(state, rs, re_)
        else:
            rows = [
                (uuid, c)
                for uuid, c in state["conversations"].items()
                if not c.get("deleted_at")
            ]

        if sig_filter:
            rows = [(u, c) for u, c in rows if _normalize_sig(c.get("significance")) == sig_filter]

        for uuid, c in rows:
            hit = _search_conversation(uuid, c, terms, search_body)
            if hit:
                results.append(hit)

    # Sort: more matches first, then by significance, then by date desc.
    results.sort(
        key=lambda r: (
            -r["match_count"],
            SIG_ORDER.get(r["significance"], 1),
            r["created_at"],
        )
    )

    results = results[:max_results]

    if not results:
        scope = f" in {args.period}" if args.period else ""
        print(f"No matches for '{' '.join(args.query)}'{scope}.")
        if not search_body:
            print("  Tip: add --body to also search summary text.")
        return

    print(f"Found {len(results)} match(es) for '{' '.join(args.query)}':\n")
    for r in results:
        sig_badge = {"high": "▲", "medium": "●", "low": "○"}.get(r["significance"], "?")
        matched = ", ".join(r["matches"])
        print(f"  {sig_badge} {r['created_at']}  {r['uuid'][:8]}  {r['title'][:60]}")
        print(f"    matched: {matched}")
        if r["topics"]:
            print(f"    topics: {r['topics']}")
        if r["tags"]:
            print(f"    tags: {r['tags']}")
        if r["summary_file"]:
            print(f"    → {r['summary_file']}")
        print()
