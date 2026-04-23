"""data/pending.md — human + Claude-readable delta of what needs doing.

Regenerated after every ingest. Three sections:
- New conversations (tracked for the first time this ingest)
- Updated conversations (previously summarized, now stale)
- Stale period entries (synthesized but covering changed conversations)
- Deleted conversations (tombstoned)

Claude Code reads this as prompt context for summarize/synthesize runs so
it knows *why* the work is happening and what changed.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from . import state as state_mod
from .paths import pending_file
from .state import now_iso


def _format_conv_line(uuid: str, c: dict[str, Any]) -> str:
    short = uuid.split("-")[0]
    created = (c.get("created_at") or "")[:10]
    project = c.get("project_name") or "general"
    title = c.get("title") or "(untitled)"
    return f"- {short} · {created} · {project} · \"{title}\""


def write_pending(
    state: dict[str, Any],
    *,
    newly_added: list[str] | None = None,
    newly_deleted: list[str] | None = None,
) -> dict[str, int]:
    """Rewrite pending.md based on current state. Returns counts for notifications."""
    newly_added = newly_added or []
    newly_deleted = newly_deleted or []

    convs = state["conversations"]

    # New: in newly_added set (first seen this ingest).
    new_rows = [
        _format_conv_line(u, convs[u])
        for u in newly_added
        if u in convs and not convs[u].get("deleted_at")
    ]

    # Updated: stale summary AND not in newly_added AND has a previous summary.
    updated_rows = []
    for uuid, c in convs.items():
        if uuid in newly_added:
            continue
        if c.get("deleted_at"):
            continue
        if c.get("summarized_at") and state_mod.summary_stale(c):
            short = uuid.split("-")[0]
            project = c.get("project_name") or "general"
            title = c.get("title") or "(untitled)"
            updated_rows.append(
                f"- {short} · last updated {c['updated_at'][:10]} · {project} · \"{title}\"\n"
                f"  (summary from {c['summarized_at'][:10]} is now stale)"
            )

    # Brand new (no prior summary) — not "updated," they go under "New" above
    # if they came in this ingest, else under "Awaiting first summary."
    awaiting_rows = []
    for uuid, c in convs.items():
        if uuid in newly_added:
            continue
        if c.get("deleted_at"):
            continue
        if not c.get("summarized_at"):
            awaiting_rows.append(_format_conv_line(uuid, c))

    # Stale entries.
    stale_entries = []
    for period_label, entry in state.get("entries", {}).items():
        rs = entry.get("range_start")
        re_ = entry.get("range_end")
        if not rs or not re_:
            continue
        if state_mod.entry_stale(state, period_label, rs, re_):
            changed = [
                u
                for u, c in state_mod.conversations_in_period(state, rs, re_)
                if c["updated_at"] > entry.get("synthesized_at", "")
            ]
            stale_entries.append(
                f"- {period_label} · {len(changed)} conversations changed since last "
                f"synthesis ({entry['synthesized_at'][:10]})"
            )

    # Deleted this ingest.
    deleted_rows = []
    for uuid in newly_deleted:
        c = convs.get(uuid, {})
        title = c.get("title") or "(unknown)"
        short = uuid.split("-")[0]
        deleted_rows.append(f"- {short} · \"{title}\"")

    # Assemble.
    lines = [
        "# Chronicle — Pending work",
        f"Generated: {now_iso()}",
        "",
    ]

    def section(header: str, rows: list[str]) -> None:
        if not rows:
            return
        lines.append(f"## {header} ({len(rows)})")
        lines.extend(rows)
        lines.append("")

    section("New conversations (need first summary)", new_rows)
    section("Updated conversations (existing summaries now stale)", updated_rows)
    section("Awaiting first summary (carried over)", awaiting_rows)
    section("Stale period entries", stale_entries)
    section("Deleted conversations (tombstoned this ingest)", deleted_rows)

    total_pending = len(new_rows) + len(updated_rows) + len(awaiting_rows) + len(stale_entries)

    path = pending_file()
    if total_pending == 0 and not deleted_rows:
        # Nothing pending and nothing to announce — remove stale file.
        if path.exists():
            path.unlink()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "new": len(new_rows),
        "updated": len(updated_rows),
        "awaiting": len(awaiting_rows),
        "stale_entries": len(stale_entries),
        "deleted": len(deleted_rows),
    }


def prune_summarized(state: dict[str, Any], uuids: list[str]) -> None:
    """After summarize runs, regenerate pending.md from the (now-fresher) state."""
    write_pending(state)


def prune_synthesized(state: dict[str, Any], period_label: str) -> None:
    write_pending(state)
