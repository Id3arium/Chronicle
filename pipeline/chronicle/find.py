"""`chronicle find` — search conversations by keyword, topic, or body text.

Uses the inverted index in data/index.json for fast weighted search.
Each query term is looked up in the inverted index, which returns matching
uuids with weights (keyword=3, topic=2, title=1). Results are scored by
summing weights across matched terms, then ranked by score → significance
→ date.

With --body, falls back to per-file reads for full-text search.
With --period, scopes to a date range (uses per-file reads).
"""

from __future__ import annotations

import re
from typing import Any

from . import state as state_mod
from .calendar import PeriodParseError, parse_period
from .index import load_index, _tokenize
from .metrics import parse_frontmatter
from .paths import data_root


SIG_ORDER = {"high": 0, "medium": 1, "med": 1, "low": 2}


def _normalize_sig(s: str | None) -> str:
    if not s:
        return "medium"
    return "medium" if s == "med" else s


def _search_inverted(
    idx: dict[str, Any],
    terms: list[str],
    sig_filter: str | None,
) -> list[dict[str, Any]]:
    """Search using the inverted index. Returns scored results.
    Score = sum of (source_weight * idf) for each matched token."""
    inverted = idx.get("inverted", {})
    idf = idx.get("idf", {})
    entries_by_uuid = {e["uuid"]: e for e in idx["entries"]}

    # Tokenize query terms the same way the index was built.
    query_tokens = []
    for term in terms:
        query_tokens.extend(_tokenize(term))
    if not query_tokens:
        query_tokens = terms  # fallback: use raw terms

    # Accumulate scores per uuid.
    scores: dict[str, dict[str, Any]] = {}  # uuid → {score, matched_terms}
    for token in query_tokens:
        hits = inverted.get(token, [])
        token_idf = idf.get(token, 1.0)
        for hit in hits:
            uuid = hit["uuid"]
            weight = hit["weight"]
            if uuid not in scores:
                scores[uuid] = {"score": 0.0, "matched_terms": set()}
            scores[uuid]["score"] += weight * token_idf
            scores[uuid]["matched_terms"].add(token)

    # Small multipliers for significance and conversation length so they
    # break ties without overriding IDF-based relevance.
    # Significance: high=1.15, medium=1.0, low=0.85
    # Length: log-scaled boost from summary word count, capped at ~1.2x
    import math
    SIG_BOOST = {"high": 1.15, "medium": 1.0, "low": 0.85}

    results = []
    for uuid, info in scores.items():
        entry = entries_by_uuid.get(uuid)
        if not entry:
            continue
        sig = _normalize_sig(entry.get("significance"))
        if sig_filter and sig != sig_filter:
            continue
        base_score = info["score"]
        sig_mult = SIG_BOOST.get(sig, 1.0)
        # log2(words/100) gives ~1.0 at 200 words, ~3.3 at 1000, ~4.6 at 2500.
        # Divide by 5 and add 1 to get a gentle 1.0–1.9 range.
        words = entry.get("summary_words") or 100
        length_mult = 1.0 + min(math.log2(max(words, 100) / 100), 5) / 5
        final_score = base_score * sig_mult * length_mult

        results.append({
            "uuid": uuid,
            "title": entry.get("title") or "(untitled)",
            "created_at": entry.get("created_at", "")[:10],
            "significance": sig,
            "topics": entry.get("topics", ""),
            "keywords": entry.get("keywords", ""),
            "categories": entry.get("categories", ""),
            "matches": sorted(info["matched_terms"]),
            "score": final_score,
            "summary_file": entry.get("summary_file", ""),
        })

    return results


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
    keywords = (fm.get("keywords") or fm.get("tags") or "").lower()
    categories = (fm.get("categories") or c.get("categories", "")).lower()
    project = (c.get("project_name") or fm.get("project") or "").lower()

    searchable = f"{title} {topics} {keywords} {categories} {project}"

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
        "keywords": fm.get("keywords") or fm.get("tags") or "",
        "categories": categories,
        "matches": matches,
        "score": len(matches),
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

    # Fast path: use inverted index.
    idx = load_index() if not search_body else None
    results = []

    if idx and idx.get("inverted") and not args.period:
        # Inverted-index search: O(1) per query token.
        results = _search_inverted(idx, terms, sig_filter)
    elif idx and not args.period:
        # Old-style index without inverted map — fall back to linear scan.
        # (Shouldn't happen after rebuild, but handles stale index files.)
        for entry in idx["entries"]:
            if sig_filter and _normalize_sig(entry.get("significance")) != sig_filter:
                continue
            searchable = " ".join([
                entry.get("title", ""),
                entry.get("topics", ""),
                entry.get("keywords", entry.get("tags", "")),
                entry.get("categories", ""),
                entry.get("project", ""),
            ]).lower()
            matches = [t for t in terms if t in searchable]
            if matches:
                results.append({
                    "uuid": entry["uuid"],
                    "title": entry.get("title") or "(untitled)",
                    "created_at": entry.get("created_at", "")[:10],
                    "significance": _normalize_sig(entry.get("significance")),
                    "topics": entry.get("topics", ""),
                    "keywords": entry.get("keywords", entry.get("tags", "")),
                    "categories": entry.get("categories", ""),
                    "matches": matches,
                    "score": len(matches),
                    "summary_file": entry.get("summary_file", ""),
                })
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

    # Sort: highest score first, then by significance, then by date desc.
    results.sort(
        key=lambda r: (
            -r["score"],
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
        score = r["score"]
        print(f"  {sig_badge} {r['created_at']}  {r['uuid'][:8]}  {r['title'][:60]}  (score: {score:.1f})")
        print(f"    matched: {matched}")
        if r["topics"]:
            print(f"    topics: {r['topics']}")
        if r["keywords"]:
            print(f"    keywords: {r['keywords']}")
        if r["summary_file"]:
            print(f"    → {r['summary_file']}")
        print()
