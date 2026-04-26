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


def parse_frontmatter(text: str) -> dict[str, str]:
    """Extract the `key: value` pairs from a leading `---\\n...\\n---` block.
    Returns {} if no frontmatter is found. Values stay as strings — caller
    converts to int/float as needed."""
    t = text.lstrip()
    if not t.startswith("---\n"):
        return {}
    rest = t[4:]
    end = rest.find("\n---")
    if end == -1:
        return {}
    block = rest[:end]
    out: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


def entry_body(text: str) -> str:
    """Return the markdown body of an entry/summary with frontmatter stripped.
    Used for entry word counts so the metrics block doesn't count itself."""
    t = text.lstrip()
    if not t.startswith("---\n"):
        return text
    rest = t[4:]
    end = rest.find("\n---")
    if end == -1:
        return text
    # Skip past the closing fence + newline.
    after = rest[end + len("\n---"):]
    return after.lstrip("\n")
