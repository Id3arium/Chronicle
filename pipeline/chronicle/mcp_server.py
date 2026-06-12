"""Chronicle MCP server — exposes search and read tools for Claude Desktop.

Run via:  uv run --project <pipeline-dir> python -m chronicle.mcp_server
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .find import _search_inverted, _normalize_sig, SIG_ORDER
from .index import load_index
from .paths import data_root, entries_dir, vault_root
from .metrics import parse_frontmatter
from . import state as state_mod
from .calendar import PeriodParseError, parse_period

mcp = FastMCP("Chronicle")


@mcp.tool()
def chronicle_find(
    query: str,
    significance: str | None = None,
    limit: int = 20,
) -> str:
    """Search Alejandro's Chronicle — a personal encyclopedia of his past
    claude.ai conversations, stored as markdown summaries with metadata.

    HOW TO USE RESULTS:
    - Always present a condensed list of the top results to Alejandro
      (one line each: date, title, score). He wants to see what matched —
      important topics often span multiple conversations.
    - Then call chronicle_read on the top hit(s) to read the full summary
      and answer the question.
    - Quote specific passages from summaries, don't paraphrase loosely.

    SEARCH TIPS:
    - Use 1–3 specific terms. "Michael Levin" beats "biology". Rare terms
      score higher (IDF weighting).
    - Multiple terms narrow results: "levin morphogenesis" scores
      conversations matching both terms much higher.
    - Keywords include associated domains, so "levin" finds bioelectricity
      conversations even if that word wasn't the search term.

    COVERAGE: Chronicle only contains conversations that were downloaded
    and processed. It is NOT a live mirror of claude.ai. If no results,
    say "no matches in Chronicle" — NOT "you never discussed X." The
    conversation may exist but not be ingested yet.

    VOCABULARY:
    - FOP = Fractal Organization Principle (Alejandro's framework)
    - STRC/MSTX = trading structures Alejandro built
    - Don't guess at acronyms — ask or leave verbatim
    - Summaries refer to Alejandro as "you" or by name, never "the user"

    Args:
        query: Space-separated search terms. 1-3 specific terms work best.
        significance: Filter by "high", "medium", or "low". Optional.
        limit: Max results to return (default 20).
    """
    terms = query.lower().split()
    if not terms:
        return "No search terms provided."

    sig_filter = None
    if significance:
        sig_filter = significance.lower()
        if sig_filter == "med":
            sig_filter = "medium"

    idx = load_index()
    if not idx or not idx.get("inverted"):
        return "Search index not found. Run `chronicle rebuild-index` first."

    results = _search_inverted(idx, terms, sig_filter)
    results.sort(
        key=lambda r: (
            -r["score"],
            SIG_ORDER.get(r["significance"], 1),
            r["created_at"],
        )
    )
    results = results[:limit]

    if not results:
        return f"No matches for '{query}'."

    lines = [f"Found {len(results)} match(es) for '{query}':\n"]
    for r in results:
        sig_badge = {"high": "▲", "medium": "●", "low": "○"}.get(
            r["significance"], "?"
        )
        matched = ", ".join(r["matches"])
        lines.append(
            f"  {sig_badge} {r['created_at']}  {r['title'][:60]}  "
            f"(score: {r['score']:.1f})"
        )
        lines.append(f"    matched: {matched}")
        if r["topics"]:
            lines.append(f"    topics: {r['topics']}")
        if r["summary_file"]:
            lines.append(f"    file: {r['summary_file']}")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
def chronicle_ls(
    period: str | None = None,
    project: str | None = None,
    significance: str | None = None,
) -> str:
    """List conversations, optionally filtered by period, project, and/or
    significance. Provide at least one filter.

    Period formats: "2026_Q1" (quarter), "2026_03_H2" (second half of
    March), "2025" (full year). For the exact project name, see
    chronicle_projects.

    Args:
        period: Period label like "2026_Q1", "2026_03_H2", "2025". Optional.
        project: Exact project name (case-insensitive), e.g. "Hermes". Optional.
        significance: Filter by "high", "medium", or "low". Optional.
    """
    if not (period or project or significance):
        return "Provide at least one filter: period, project, or significance."

    state = state_mod.load()

    if period:
        try:
            _tier, rs, re_ = parse_period(period)
        except PeriodParseError as e:
            return str(e)
        rows = state_mod.conversations_in_period(state, rs, re_)
        scope = f"{period} ({rs} → {re_})"
    else:
        rows = [
            (u, c) for u, c in state["conversations"].items()
            if not c.get("deleted_at")
        ]
        scope = "all conversations"

    if project:
        pl = project.lower()
        rows = [
            (u, c) for u, c in rows
            if (c.get("project_name") or "").lower() == pl
        ]
        scope = f"{project}" + (f" in {period}" if period else "")

    if significance:
        sig_filter = significance.lower()
        if sig_filter == "med":
            sig_filter = "medium"
        rows = [
            (u, c) for u, c in rows if c.get("significance") == sig_filter
        ]

    rows = sorted(rows, key=lambda x: x[1].get("created_at") or "")

    if not rows:
        return f"No conversations matching {scope}."

    lines = [f"{scope} — {len(rows)} conversation(s)\n"]
    for uuid, c in rows:
        date = (c.get("created_at") or "")[:10]
        sig = {"high": "▲", "medium": "●", "low": "○"}.get(
            c.get("significance", ""), "?"
        )
        title = c.get("title") or "(untitled)"
        sf = c.get("summary_file") or ""
        lines.append(f"  {sig} {date}  {title[:60]}")
        if sf:
            lines.append(f"    file: {sf}")

    return "\n".join(lines)


@mcp.tool()
def chronicle_projects() -> str:
    """List all Claude.ai projects with their conversation counts.

    Use this to find the exact project name before filtering with
    chronicle_ls(project=...). Names are case-sensitive in display but
    the filter is case-insensitive.
    """
    from collections import Counter
    state = state_mod.load()
    counts: Counter = Counter()
    for uuid, c in state["conversations"].items():
        if c.get("deleted_at"):
            continue
        name = c.get("project_name") or "(no project)"
        counts[name] += 1

    if not counts:
        return "No conversations found."

    lines = ["Projects (by conversation count):\n"]
    for name, n in counts.most_common():
        lines.append(f"  {n:>4}  {name}")
    return "\n".join(lines)


@mcp.tool()
def chronicle_cards(
    uuids: list[str] | None = None,
    period: str | None = None,
    project: str | None = None,
    significance: str | None = None,
    limit: int = 50,
) -> str:
    """Scan conversation INDEX CARDS — the compact metadata for each
    conversation (date, title, project, significance, topics, keywords)
    without the full summary body.

    This is the cheap middle tier between chronicle_ls (just titles) and
    chronicle_read (the whole summary). Use it to BROWSE many conversations
    at once and decide which one to drill into — then chronicle_read its
    summary_file, or chronicle_passage the transcript.

    TWO WAYS TO CALL:

    1. BY UUID LIST — pass `uuids` (full or prefix) to pull cards for a
       specific shortlist. Ideal right after chronicle_find: grab the cards
       for the top hits to compare their topics/keywords side by side
       before committing to read one.

    2. BY FILTER — pass any of `period`, `project`, `significance` to list
       all matching cards. Good for serendipitous revisiting, e.g.
       chronicle_cards(period="2025_Q3", significance="high") to surface
       past high-significance conversations worth a second look.

    Provide a uuid list OR at least one filter. Cards are chronological.

    Args:
        uuids: Conversation UUIDs or prefixes to fetch cards for. Optional.
        period: Period label like "2026_Q1", "2026_03_H2", "2025". Optional.
        project: Project name (case-insensitive). See chronicle_projects.
        significance: Filter by "high", "medium", or "low". Optional.
        limit: Max cards to return in filter mode (default 50).
    """
    if not (uuids or period or project or significance):
        return "Provide a uuid list, or at least one filter (period, project, significance)."

    idx = load_index()
    cards = idx.get("entries", []) if idx else []
    if not cards:
        return "No index found. Run `chronicle index` first."

    idf = idx.get("idf", {})

    sig_filter = None
    if significance:
        sig_filter = significance.lower()
        if sig_filter == "med":
            sig_filter = "medium"

    if uuids:
        prefixes = [u.lower() for u in uuids]
        selected = [
            c for c in cards
            if any((c.get("uuid") or "").lower().startswith(p) for p in prefixes)
        ]
        scope = f"{len(selected)} card(s) for {len(uuids)} requested uuid(s)"
    else:
        rs = re_ = None
        if period:
            try:
                _tier, rs, re_ = parse_period(period)
            except PeriodParseError as e:
                return str(e)
        pl = project.lower() if project else None
        selected = []
        for c in cards:
            date = (c.get("created_at") or "")[:10]
            if rs is not None and not (rs <= date <= re_):
                continue
            if pl is not None and (c.get("project") or "").lower() != pl:
                continue
            if sig_filter is not None and c.get("significance") != sig_filter:
                continue
            selected.append(c)
        bits = [b for b in (period, project, significance) if b]
        scope = f"{len(selected)} card(s) — {', '.join(bits)}"

    if uuids and sig_filter is not None:
        selected = [c for c in selected if c.get("significance") == sig_filter]

    selected.sort(key=lambda c: c.get("created_at") or "")
    truncated = len(selected) > limit
    selected = selected[:limit]

    if not selected:
        return "No matching cards."

    lines = [scope + (f" (showing first {limit})" if truncated else "") + ":\n"]
    for c in selected:
        sig = {"high": "▲", "medium": "●", "low": "○"}.get(
            c.get("significance", ""), "?"
        )
        date = (c.get("created_at") or "")[:10]
        title = c.get("title") or "(untitled)"
        proj = c.get("project") or ""
        proj_tag = f"  [{proj}]" if proj and proj != "general" else ""
        lines.append(f"  {sig} {date}  {title[:60]}{proj_tag}")
        if c.get("topics"):
            lines.append(f"    topics: {c['topics']}")
        if c.get("keywords"):
            kws = [k.strip() for k in c["keywords"].split(",") if k.strip()]
            ranked = _rank_keywords(kws, idf)
            shown_kws = ", ".join(ranked[:12])
            if len(ranked) > 12:
                shown_kws += f", … (+{len(ranked) - 12} more)"
            lines.append(f"    keywords: {shown_kws}")
        if c.get("summary_file"):
            lines.append(f"    file: {c['summary_file']}")
        lines.append("")

    return "\n".join(lines).rstrip()


@mcp.tool()
def chronicle_read(path: str) -> str:
    """Read a Chronicle summary or entry file by its path.

    Use the file paths returned by chronicle_find, chronicle_ls, or
    chronicle_entries. Paths can be relative (e.g.
    "summaries/2026-02/27_the-misbehavior-of-markets__322ed80e.md")
    or absolute.

    Summaries are markdown files with YAML frontmatter (title, topics,
    keywords, significance, etc.) followed by the summary body.

    Entries are period syntheses (half-month, quarter, year) that
    aggregate multiple conversation summaries into a narrative.

    Args:
        path: Relative path from find/ls/entries output, or absolute path.
    """
    p = Path(path)
    if not p.is_absolute():
        p = vault_root() / p

    if not p.exists():
        return f"File not found: {p}"
    if not p.suffix == ".md":
        return f"Not a markdown file: {p}"

    # Safety: only serve files inside the vault
    try:
        p.resolve().relative_to(vault_root().resolve())
    except ValueError:
        return f"Access denied: path is outside the Chronicle library."

    return p.read_text(encoding="utf-8")


@mcp.tool()
def chronicle_passage(
    conversation: str,
    query: str | None = None,
    index: int | None = None,
    before: int = 0,
    after: int = 0,
    limit: int = 10,
) -> str:
    """Retrieve verbatim messages from a raw conversation transcript.

    Use after reading a summary when you need the EXACT words — direct
    quotes, precise phrasing, the actual back-and-forth. Messages are
    numbered by position (0-based) and always returned in chronological
    order.

    TWO MODES:

    1. KEYWORD SEARCH — pass `query`. Finds every message containing any
       of the keywords, in chronological order, and returns the first
       `limit` of them (default 10). The FIRST hit in the output is the
       first occurrence in the conversation. Each result is labelled with
       its position, e.g. "message 40/124".

       → "Find the first occurrence of X" = call with query=X; the first
         result is the answer.

    2. INDEX LOOKUP — pass `index` to fetch a specific message by
       position. Optionally add `before=N` and/or `after=N` to include
       neighbouring messages — DIRECTIONALLY. Each is capped at 5.
         before=5  → the 5 messages before index (the lead-up)
         after=5   → the 5 messages after index (what followed)
       Default before=0, after=0 returns just that one message.

    Typical flow for "find first occurrence, then show what led up to it":
      a. chronicle_passage(conv, query="the phrase")
         → first hit is at, say, message 40.
      b. chronicle_passage(conv, index=40, before=5)
         → messages 35–40, the lead-up (you already have 40, so it's the
           tail of this range).
    To see what came AFTER instead, use after=5. To see both sides, set
    both. Pull only the direction you need; widen only if it's not enough.

    Args:
        conversation: UUID (or prefix) of the conversation, or the
            relative path to the conversation JSON file
            (e.g. "conversations/2026-02/27_markets__322ed80e.json").
        query: Space-separated keywords. Matches any message containing
            ANY keyword (case-insensitive). Provide query OR index.
        index: Message position (0-based) to retrieve. Provide query OR index.
        before: In index mode, also include this many messages BEFORE
            `index` (the lead-up). Default 0. Capped at 5.
        after: In index mode, also include this many messages AFTER
            `index` (what followed). Default 0. Capped at 5.
        limit: In keyword mode, max matching messages to return (default 10).
    """
    conv_path = _resolve_conversation(conversation)
    if conv_path is None:
        return f"Conversation not found: {conversation}"

    try:
        data = json.loads(conv_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return f"Error reading conversation: {e}"

    messages = data.get("messages", [])
    if not messages:
        return "No messages in this conversation."

    total = len(messages)

    # Index mode: return a message (optionally with directional neighbours)
    if index is not None:
        if index < 0 or index >= total:
            return f"Index {index} out of range (0–{total - 1})."
        before = max(0, min(before, 5))
        after = max(0, min(after, 5))
        lo = max(0, index - before)
        hi = min(total - 1, index + after)
        lines = []
        if before or after:
            lines.append(f"Messages {lo}–{hi} (of {total} total):\n")
        for i in range(lo, hi + 1):
            msg = messages[i]
            role = msg.get("sender", "unknown")
            text = _extract_text(msg)
            display = text if len(text) <= 3000 else text[:3000] + "… [truncated]"
            marker = " ← requested" if i == index and (before or after) else ""
            lines.append(f"--- message {i}/{total - 1} ({role}){marker} ---")
            lines.append(display)
            lines.append("")
        return "\n".join(lines).rstrip()

    # Keyword mode
    if not query:
        return "Provide either query (keywords) or index (message position)."

    terms = query.lower().split()
    if not terms:
        return "No search terms provided."

    hits = []
    for i, msg in enumerate(messages):
        text = _extract_text(msg)
        text_lower = text.lower()
        matched = [t for t in terms if t in text_lower]
        if matched:
            role = msg.get("sender", "unknown")
            hits.append((i, role, text, matched))

    if not hits:
        return f"No messages matching '{query}' in this conversation."

    shown = hits[:limit]
    more = len(hits) - len(shown)
    header = f"Found {len(hits)} matching message(s) (of {total} total), in order:"
    if more > 0:
        header += f" showing first {len(shown)}, {more} more not shown."
    lines = [header + "\n"]
    for i, role, text, matched in shown:
        display = text if len(text) <= 2000 else text[:2000] + "… [truncated]"
        lines.append(f"--- message {i}/{total - 1} ({role}) [matched: {', '.join(matched)}] ---")
        lines.append(display)
        lines.append("")

    return "\n".join(lines)


def _rank_keywords(keywords: list[str], idf: dict[str, float]) -> list[str]:
    """Order keyword phrases most-central first (the topical anchors), so a
    truncated card preview keeps what the conversation is ABOUT rather than
    its rarest incidental terms.

    A phrase is scored by its minimum token IDF — it is as common as its most
    common word — and sorted ascending. Phrases whose tokens are all unknown
    to the index fall to the end (treated as maximally rare). Stable, so the
    original order breaks ties.
    """
    MAX = 99.0

    def score(phrase: str) -> float:
        toks = [t for t in phrase.lower().split() if t in idf]
        return min((idf[t] for t in toks), default=MAX)

    return sorted(keywords, key=score)


def _extract_text(msg: dict) -> str:
    """Extract plain text from a message's content field."""
    content = msg.get("content", "")
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return str(content)


def _resolve_conversation(ref: str) -> Path | None:
    """Resolve a conversation reference to a file path.

    Accepts: UUID, UUID prefix, or relative file path.
    """
    # Direct path
    if "/" in ref or ref.endswith(".json"):
        p = Path(ref)
        if not p.is_absolute():
            p = vault_root() / p
        return p if p.exists() else None

    # UUID lookup via state
    state = state_mod.load()
    convos = state.get("conversations", {})

    # Exact match
    if ref in convos:
        cf = convos[ref].get("conversation_file")
        if cf:
            p = vault_root() / cf
            return p if p.exists() else None

    # Prefix match
    ref_lower = ref.lower()
    for uuid, c in convos.items():
        if uuid.lower().startswith(ref_lower):
            cf = c.get("conversation_file")
            if cf:
                p = vault_root() / cf
                return p if p.exists() else None

    return None


@mcp.tool()
def chronicle_entries(year: str | None = None) -> str:
    """List Chronicle entry files — period syntheses (half-month, quarter,
    year) that aggregate conversation summaries into narratives.

    Use this to find entry files, then chronicle_read to read them.
    Useful for questions like "what happened in Q1 2026?" or "show me
    the 2025 yearly entry."

    Entry hierarchy:
    - Half-month entries (H1 = days 1-15, H2 = days 16-end)
    - Quarter entries (Q1-Q4, synthesize 6 half-month entries)
    - Year entries (synthesize 4 quarter entries)

    Args:
        year: Optional year to filter (e.g. "2025"). Shows all years if omitted.
    """
    base = entries_dir()
    if not base.exists():
        return "No entries directory found."

    lines = []
    for year_dir in sorted(base.iterdir()):
        if not year_dir.is_dir():
            continue
        if year and year_dir.name != year:
            continue

        # Yearly entry sits directly in the year dir
        for f in sorted(year_dir.glob("*_Entry.md")):
            lines.append(f"  {f.relative_to(vault_root())}")

        # Quarter subdirs
        for q_dir in sorted(year_dir.iterdir()):
            if not q_dir.is_dir() or not q_dir.name.startswith("Q"):
                continue
            for f in sorted(q_dir.glob("*_Entry.md")):
                lines.append(f"    {f.relative_to(vault_root())}")

    if not lines:
        return f"No entries found{' for ' + year if year else ''}."

    header = f"Chronicle entries{' for ' + year if year else ''}:\n"
    return header + "\n".join(lines)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
