"""Obsidian wikilink emission for tree navigation.

Every file in the fractal tree gets two kinds of link, Wikipedia-style:

  - parent ("up"), as the FIRST body line:
      summary  : (none at write time — synthesize stamps it; see below)
      entry    : `Part of [[<parent entry>]]`  (year has no parent)
  - children ("down"), as a section at the BOTTOM of the body:
      summary  : `## Full conversation` → [[<uuid>.json]]
      entry    : `## Sources`           → [[<child stem>]] bullets

Wikilinks resolve by basename. Markdown targets are linked WITHOUT the
`.md` extension (`[[2026_Q2_Entry]]`); the conversation JSON is not
markdown so it keeps its extension (`[[abc.json]]`) — Obsidian treats it
as a non-rendered leaf, which is fine.

Parent links on *summaries* are deliberately NOT written by summarize:
the parent label depends on the sparse-month merge decision, which only
synthesize knows. synthesize stamps `Part of [[…]]` into each child it
consumes, so the link is always correct (incl. the merged H1-H2 case)
and self-heals on re-synthesis. See `set_parent_link`.
"""

from __future__ import annotations

from pathlib import Path

from .metrics import render_with_frontmatter, split_frontmatter

_PARENT_PREFIX = "Part of [["
_FULL_CONVO_HEADING = "## Full conversation"
_SOURCES_HEADING = "## Sources"


def _stem(name: str) -> str:
    """Basename without a trailing .md (Obsidian resolves md by stem)."""
    return name[:-3] if name.endswith(".md") else name


def wikilink_md(rel_or_name: str) -> str:
    """`[[stem]]` for a markdown target (path or bare name accepted)."""
    return f"[[{_stem(Path(rel_or_name).name)}]]"


def wikilink_file(rel_or_name: str) -> str:
    """`[[name.ext]]` for a non-markdown target (keeps extension)."""
    return f"[[{Path(rel_or_name).name}]]"


def _strip_existing_parent(body: str) -> str:
    """Drop a leading `Part of [[…]]` line (and the blank after it) so the
    parent link can be re-stamped idempotently / corrected on re-synthesis."""
    lines = body.split("\n")
    if lines and lines[0].startswith(_PARENT_PREFIX):
        i = 1
        while i < len(lines) and lines[i].strip() == "":
            i += 1
        return "\n".join(lines[i:])
    return body


def set_parent_link(text: str, parent_md_name: str) -> str:
    """Return `text` with `Part of [[parent]]` as the first body line.
    Idempotent: replaces any existing parent line rather than stacking.
    Frontmatter is preserved exactly (parse → reserialize, never splice)."""
    fields, body = split_frontmatter(text)
    body = _strip_existing_parent(body)
    new_body = f"Part of {wikilink_md(parent_md_name)}\n\n{body}"
    if fields:
        return render_with_frontmatter(fields, new_body)
    return new_body


def _drop_section(body: str, heading: str) -> str:
    """Remove a trailing `---`-fenced `## heading` block (and the one we
    add) so the down-section can be regenerated idempotently."""
    lines = body.rstrip().split("\n")
    for i, ln in enumerate(lines):
        if ln.strip() == heading:
            cut = i
            # also swallow a `---` separator immediately above it
            j = i - 1
            while j >= 0 and lines[j].strip() == "":
                j -= 1
            if j >= 0 and lines[j].strip() == "---":
                cut = j
            return "\n".join(lines[:cut]).rstrip()
    return body.rstrip()


def set_down_section(text: str, heading: str, link_lines: list[str]) -> str:
    """Replace/append a bottom `## heading` section of wikilink bullets.
    `link_lines` are already-formatted bullet strings (without leading
    `- `). Idempotent: an existing section with this heading is removed
    first. Frontmatter preserved via parse → reserialize."""
    fields, body = split_frontmatter(text)
    base = _drop_section(body, heading)
    bullets = "\n".join(f"- {l}" for l in link_lines)
    new_body = f"{base}\n\n---\n\n{heading}\n\n{bullets}\n"
    if fields:
        return render_with_frontmatter(fields, new_body)
    return new_body


def set_full_conversation(text: str, conversation_rel: str) -> str:
    """Bottom `## Full conversation` section linking the raw JSON leaf."""
    return set_down_section(
        text, _FULL_CONVO_HEADING, [wikilink_file(conversation_rel)]
    )


def set_sources(text: str, child_md_names: list[str]) -> str:
    """Bottom `## Sources` section of wikilinks to child entries/summaries."""
    return set_down_section(
        text, _SOURCES_HEADING, [wikilink_md(n) for n in child_md_names]
    )
