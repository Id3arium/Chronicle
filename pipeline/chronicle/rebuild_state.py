"""`chronicle rebuild-state` — reconstruct state.json from files on disk.

Scans conversations/, summaries/, and entries/ directories to rebuild the
full state. This is the safety net: if state.json is ever lost, corrupted,
or drifted, this command recovers everything from the authoritative files.

What it recovers:
- conversations: uuid, title, created_at, updated_at, project_name,
  conversation_file, summary_file, summarized_at, significance, metrics
- entries: period label, tier, range, synthesized_at, metrics
- processed_imports: filenames from inbox that exist on disk

What it CANNOT recover (information only in state.json):
- first_seen timestamps (set to created_at as fallback)
- deleted_at for soft-deleted conversations (scans deleted/ dirs)
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .metrics import (
    compression_ratio,
    conversation_prose,
    measure_text,
    parse_frontmatter,
)
from .paths import (
    conversations_dir,
    deleted_conversations_dir,
    deleted_summaries_dir,
    entries_dir,
    inbox_dir,
    stem_for,
    summaries_dir,
)


_UUID_RE = re.compile(r"__([0-9a-f]{8})\.(?:json|md)$")


def _extract_uuid_prefix(filename: str) -> str | None:
    """Extract the 8-char UUID prefix from a stem like '04_some-title__b516bcda'."""
    m = _UUID_RE.search(filename)
    return m.group(1) if m else None


def _find_full_uuid(prefix: str, conv_data: dict) -> str | None:
    """Given an 8-char prefix, find the full UUID from conversation JSON."""
    return conv_data.get("uuid")


def _scan_conversations(conv_dir: Path, deleted: bool = False) -> dict[str, dict[str, Any]]:
    """Scan conversation JSON files. Returns {uuid: record}.

    Handles two layouts:
    - conversations/YYYY-MM/stem.json  (normal)
    - conversations/deleted/stem.json  (flat deleted dir)
    """
    records: dict[str, dict[str, Any]] = {}
    if not conv_dir.exists():
        return records

    # Collect all .json files: either directly in conv_dir or in subdirectories.
    json_files: list[tuple[Path, str]] = []  # (path, relative_path_from_vault)
    for child in sorted(conv_dir.iterdir()):
        if child.is_file() and child.name.endswith(".json"):
            # File directly in the deleted dir — no month subdir needed.
            json_files.append((child, f"conversations/deleted/{child.name}"))
        elif child.is_dir() and child.name != "deleted":
            for conv_file in sorted(child.iterdir()):
                if conv_file.name.endswith(".json"):
                    json_files.append((
                        conv_file,
                        f"conversations/{child.name}/{conv_file.name}",
                    ))

    for conv_file, rel_path in json_files:
        try:
            with conv_file.open("r", encoding="utf-8") as f:
                conv = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        uuid = conv.get("uuid")
        if not uuid:
            continue

        # Metrics from conversation prose.
        prose = conversation_prose(conv)
        prose_metrics = measure_text(prose)
        conv_chars = conv_file.stat().st_size

        record: dict[str, Any] = {
            "title": conv.get("title"),
            "created_at": conv.get("created_at"),
            "updated_at": conv.get("updated_at"),
            "project_name": conv.get("project_name"),
            "conversation_file": rel_path,
            "conversation_chars": conv_chars,
            "summary_file": None,
            "summarized_at": None,
            "deleted_at": None,
            "first_seen": conv.get("created_at"),  # best guess
            "original_chars": prose_metrics["chars"],
            "original_words": prose_metrics["words"],
            "original_tokens_est": prose_metrics["tokens_est"],
        }
        if deleted:
            # Use file mtime as a rough deleted_at.
            mtime = conv_file.stat().st_mtime
            from datetime import datetime, timezone
            record["deleted_at"] = datetime.fromtimestamp(
                mtime, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")

        records[uuid] = record

    return records


def _scan_summaries(sum_dir: Path, conversations: dict[str, dict[str, Any]]) -> None:
    """Scan summary .md files and attach them to conversation records in-place."""
    if not sum_dir.exists():
        return

    for month_dir in sorted(sum_dir.iterdir()):
        if not month_dir.is_dir():
            continue
        if month_dir.name == "deleted":
            continue
        for sum_file in sorted(month_dir.iterdir()):
            if not sum_file.name.endswith(".md"):
                continue
            try:
                text = sum_file.read_text(encoding="utf-8")
            except OSError:
                continue

            fm = parse_frontmatter(text)
            uuid = fm.get("uuid")
            # Frontmatter uuid might be truncated, wrong, or missing.
            # Fall back to the 8-char suffix in the filename (__abcd1234.md).
            if not uuid or uuid not in conversations:
                prefix = _extract_uuid_prefix(sum_file.name)
                if prefix:
                    matches = [u for u in conversations if u.startswith(prefix)]
                    uuid = matches[0] if len(matches) == 1 else None
            if not uuid or uuid not in conversations:
                continue

            c = conversations[uuid]
            rel_path = f"summaries/{month_dir.name}/{sum_file.name}"
            c["summary_file"] = rel_path

            # Metrics from summary text.
            summary_metrics = measure_text(text)
            c["summary_chars"] = summary_metrics["chars"]
            c["summary_words"] = summary_metrics["words"]
            c["summary_tokens_est"] = summary_metrics["tokens_est"]

            # Frontmatter fields.
            c["summarized_at"] = fm.get("summarized_at")
            c["significance"] = fm.get("significance")
            c["model"] = fm.get("model")

            # Compression ratio.
            orig_chars = c.get("original_chars", 0)
            if orig_chars:
                c["compression_ratio"] = compression_ratio(
                    summary_metrics["chars"], orig_chars
                )


def _scan_entries(ent_dir: Path) -> dict[str, dict[str, Any]]:
    """Scan entry .md files. Returns {period_label: record}."""
    records: dict[str, dict[str, Any]] = {}
    if not ent_dir.exists():
        return records

    for entry_file in sorted(ent_dir.iterdir()):
        if not entry_file.name.endswith("_Entry.md"):
            continue
        # Label is filename minus '_Entry.md'.
        label = entry_file.name.removesuffix("_Entry.md")
        try:
            text = entry_file.read_text(encoding="utf-8")
        except OSError:
            continue

        fm = parse_frontmatter(text)
        entry_metrics = measure_text(text)

        record: dict[str, Any] = {
            "entry_file": f"entries/{entry_file.name}",
            "synthesized_at": fm.get("synthesized_at"),
            "range_start": fm.get("range_start"),
            "range_end": fm.get("range_end"),
            "entry_chars": entry_metrics["chars"],
            "entry_words": int(fm["entry_words"]) if fm.get("entry_words") else entry_metrics["words"],
            "model": fm.get("model"),
        }
        # Optional metrics that may be in frontmatter.
        for key in (
            "tier",
            "input_count",
            "total_source_conversation_words",
            "total_source_summary_words",
            "aggregate_source_compression_ratio",
            "entry_compression_ratio",
        ):
            if fm.get(key):
                # Try numeric conversion for numeric fields.
                val = fm[key]
                try:
                    val = int(val)
                except ValueError:
                    try:
                        val = float(val)
                    except ValueError:
                        pass
                record[key] = val

        records[label] = record

    return records


def _scan_processed_imports(inbox: Path) -> list[str]:
    """List filenames in inbox/ — these have been processed at some point."""
    if not inbox.exists():
        return []
    return sorted(f.name for f in inbox.iterdir() if f.is_file() and f.name.endswith(".json"))


def rebuild(*, dry_run: bool = False) -> dict[str, Any]:
    """Reconstruct state from disk. Returns the new state dict.

    If dry_run is False, also saves to state.json.
    """
    from . import state as state_mod

    print("Scanning conversations...")
    conversations = _scan_conversations(conversations_dir())

    # Scan deleted conversations and merge.
    deleted = _scan_conversations(deleted_conversations_dir(), deleted=True)
    for uuid, rec in deleted.items():
        if uuid not in conversations:
            conversations[uuid] = rec

    print(f"  Found {len(conversations)} conversations ({len(deleted)} deleted)")

    print("Scanning summaries...")
    _scan_summaries(summaries_dir(), conversations)
    summarized = sum(1 for c in conversations.values() if c.get("summarized_at"))
    print(f"  Matched {summarized} summaries to conversations")

    print("Scanning entries...")
    entries = _scan_entries(entries_dir())
    print(f"  Found {len(entries)} entries")

    print("Scanning inbox...")
    processed = _scan_processed_imports(inbox_dir())
    print(f"  Found {len(processed)} import files")

    # Find latest ingest timestamp.
    last_ingest = None
    for c in conversations.values():
        fs = c.get("first_seen") or c.get("created_at")
        if fs and (last_ingest is None or fs > last_ingest):
            last_ingest = fs

    state: dict[str, Any] = {
        "last_ingest": last_ingest,
        "conversations": conversations,
        "entries": entries,
        "processed_imports": processed,
        "processed_exports": processed,  # backward compat
    }

    if not dry_run:
        state_mod.save(state)
        print(f"\nState saved to {state_mod.state_file()}")
    else:
        print("\nDry run — state NOT saved.")

    return state


def run(args: Any) -> None:
    """CLI entry point."""
    dry_run = getattr(args, "dry_run", False)
    compare = getattr(args, "compare", False)

    if compare:
        from . import state as state_mod
        old = state_mod.load()
        new = rebuild(dry_run=True)
        _compare(old, new)
    else:
        rebuild(dry_run=dry_run)


def _compare(old: dict[str, Any], new: dict[str, Any]) -> None:
    """Print a diff between existing state and rebuilt state."""
    old_convos = set(old.get("conversations", {}).keys())
    new_convos = set(new.get("conversations", {}).keys())
    old_entries = set(old.get("entries", {}).keys())
    new_entries = set(new.get("entries", {}).keys())

    print("\n── Comparison ──")
    print(f"Conversations: {len(old_convos)} existing → {len(new_convos)} rebuilt")
    if missing := old_convos - new_convos:
        print(f"  ⚠ {len(missing)} in state but not on disk (ghost records)")
    if added := new_convos - old_convos:
        print(f"  + {len(added)} on disk but not in state")

    print(f"Entries: {len(old_entries)} existing → {len(new_entries)} rebuilt")
    if missing_e := old_entries - new_entries:
        print(f"  ⚠ {len(missing_e)} in state but not on disk: {', '.join(sorted(missing_e))}")
    if added_e := new_entries - old_entries:
        print(f"  + {len(added_e)} on disk but not in state: {', '.join(sorted(added_e))}")

    # Check for summarized_at drift.
    drift = 0
    for uuid in old_convos & new_convos:
        old_sat = old["conversations"][uuid].get("summarized_at")
        new_sat = new["conversations"][uuid].get("summarized_at")
        if old_sat != new_sat:
            drift += 1
    if drift:
        print(f"  ⚠ {drift} conversations have summarized_at mismatch")

    if not missing and not added and not missing_e and not added_e and not drift:
        print("  ✓ State matches disk perfectly")
