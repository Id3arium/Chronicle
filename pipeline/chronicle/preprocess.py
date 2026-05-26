"""Conversation preprocessing for summarization.

Two responsibilities:
1. Strip non-essential content (tool_use inputs, thinking blocks) to reduce
   token count while preserving conversational meaning.
2. Chunk oversized conversations for sliding-window summarization.
"""

from __future__ import annotations

import json
from pathlib import Path

# Roughly 4 chars per token. 160k token budget in a 200k context window
# leaves ~40k tokens for the instruction prompt (~3k) and output (~37k).
# The model's actual output tends to cap around ~2k words (~2.5k tokens)
# regardless of available budget, so the headroom is more than sufficient.
CHARS_PER_TOKEN = 4
MAX_TOKENS = 160_000
MAX_CHARS = MAX_TOKENS * CHARS_PER_TOKEN  # 640,000 chars


def strip_conversation(conv_json: str) -> str:
    """Strip tool_use inputs and thinking blocks from a conversation JSON.

    Keeps:
    - All `text` content blocks (the actual conversation).
    - `tool_result` text (what tools returned — usually short, often meaningful).
    - A one-line stub for each `tool_use` block: just the tool name so the
      summary knows a tool was invoked, without the full code/input payload.

    Drops:
    - `tool_use` input content (code sent to visualization/execution tools).
    - `thinking` blocks (Claude's internal chain-of-thought).
    - `artifact` blocks (stored separately by claude.ai, rarely present).

    Returns the stripped conversation as a JSON string.
    """
    conv = json.loads(conv_json)
    for msg in conv.get("messages", []) or []:
        filtered = []
        for block in msg.get("content", []) or []:
            btype = block.get("type")
            if btype == "text":
                filtered.append(block)
            elif btype == "tool_result":
                # Keep tool results — they're usually short and carry meaning.
                filtered.append(block)
            elif btype == "tool_use":
                # Replace the full tool invocation with a stub.
                name = block.get("name") or "unknown_tool"
                filtered.append({
                    "type": "text",
                    "text": f"[Used tool: {name}]",
                })
            # Drop: thinking, artifact, anything else
        msg["content"] = filtered
    return json.dumps(conv, ensure_ascii=False)


def estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def needs_chunking(conv_text: str, *, significance: str | None = None) -> bool:
    """True if this conversation exceeds the per-call context budget.

    Only chunks when the input literally doesn't fit. The model's output
    length (~1-2k words) is well under the available output token budget
    regardless of input size, so there's no output-side reason to chunk.
    """
    return estimate_tokens(conv_text) > MAX_TOKENS


def chunk_messages(conv_json: str, max_chars: int = MAX_CHARS) -> list[str]:
    """Split a conversation into chunks that fit within the token budget.

    Each chunk is a valid conversation JSON with a subset of messages.
    Chunks split on message boundaries (never mid-message). Metadata
    (uuid, title, created_at, etc.) is preserved in every chunk.

    Returns a list of JSON strings, one per chunk.
    """
    conv = json.loads(conv_json)
    messages = conv.get("messages", []) or []

    # Measure per-message size.
    msg_sizes = [len(json.dumps(m, ensure_ascii=False)) for m in messages]

    # Build the shell (everything except messages) to know its overhead.
    shell = {k: v for k, v in conv.items() if k != "messages"}
    shell_overhead = len(json.dumps(shell, ensure_ascii=False)) + 20  # for "messages":[]

    budget = max_chars - shell_overhead
    if budget <= 0:
        # Shouldn't happen, but degrade gracefully.
        return [conv_json]

    chunks: list[str] = []
    chunk_start = 0
    chunk_size = 0

    for i, size in enumerate(msg_sizes):
        if chunk_size + size > budget and chunk_start < i:
            # Flush current chunk.
            chunk_conv = dict(shell)
            chunk_conv["messages"] = messages[chunk_start:i]
            chunks.append(json.dumps(chunk_conv, ensure_ascii=False))
            chunk_start = i
            chunk_size = 0
        chunk_size += size

    # Final chunk.
    if chunk_start < len(messages):
        chunk_conv = dict(shell)
        chunk_conv["messages"] = messages[chunk_start:]
        chunks.append(json.dumps(chunk_conv, ensure_ascii=False))

    return chunks if chunks else [conv_json]
