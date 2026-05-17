"""Length metrics for conversations and summaries.

We count three things:
- chars: len(text). Cheap, deterministic, no deps.
- words: whitespace-delimited token count. Human-readable.
- tokens: chars // 4 estimate. No tokenizer dep. Close enough for ratios.

For conversations, we measure the concatenated *prose* (sender + text only)
— not the JSON wrapper. That's the apples-to-apples baseline against the
summary, which is also prose.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CHARS_PER_TOKEN = 4


def measure_text(text: str) -> dict[str, int]:
    chars = len(text)
    return {
        "chars": chars,
        "words": len(text.split()),
        "tokens_est": chars // CHARS_PER_TOKEN,
    }


def conversation_prose(conv: dict[str, Any]) -> str:
    """Concatenate the human-readable prose of a conversation. Skips JSON
    overhead, keeps sender labels so totals reflect a real readable transcript."""
    parts: list[str] = []
    for msg in conv.get("messages", []) or []:
        sender = msg.get("sender", "?")
        for block in msg.get("content", []) or []:
            if block.get("type") == "text":
                t = block.get("text") or ""
                if t:
                    parts.append(f"{sender}: {t}")
    return "\n\n".join(parts)


def measure_conversation_file(path: Path) -> dict[str, int]:
    """Read a conversation JSON from disk and return metrics over its prose."""
    with path.open("r", encoding="utf-8") as f:
        conv = json.load(f)
    return measure_text(conversation_prose(conv))


def compression_ratio(summary_chars: int, original_chars: int) -> float:
    if not original_chars:
        return 0.0
    return round(summary_chars / original_chars, 4)


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a document into (frontmatter_dict, body).

    The closing fence is the first line that is *exactly* `---` (after the
    opening `---\\n`). This is the single source of truth for "where does
    frontmatter end" — it does NOT substring-search for `\\n---`, so a `---`
    thematic break inside the prose body can never be mistaken for the
    closing fence. If there is no valid frontmatter block, returns
    ({}, original_text_unchanged).

    Frontmatter values stay as strings; callers convert as needed. Key
    insertion order follows the source block.
    """
    t = text.lstrip()
    if not t.startswith("---\n"):
        return {}, text
    lines = t[4:].split("\n")
    close_idx = None
    for i, line in enumerate(lines):
        if line.strip() == "---":
            close_idx = i
            break
    if close_idx is None:
        return {}, text
    out: dict[str, str] = {}
    for line in lines[:close_idx]:
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    body = "\n".join(lines[close_idx + 1:]).lstrip("\n")
    return out, body


def render_with_frontmatter(fields: dict[str, Any], body: str) -> str:
    """Inverse of split_frontmatter: serialize a `---` block from `fields`
    (in dict insertion order) followed by the body. Deterministic — no
    string splicing into existing output."""
    block = "".join(f"{k}: {v}\n" for k, v in fields.items())
    return f"---\n{block}---\n\n{body}"


def parse_frontmatter(text: str) -> dict[str, str]:
    """Extract the `key: value` pairs from the leading frontmatter block.
    Returns {} if none. Thin wrapper over split_frontmatter."""
    return split_frontmatter(text)[0]


def entry_body(text: str) -> str:
    """Return the markdown body with frontmatter stripped. Used for entry
    word counts so the metrics block doesn't count itself."""
    fm, body = split_frontmatter(text)
    return body if fm or body != text else text
