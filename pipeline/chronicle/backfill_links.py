"""One-shot: retrofit Obsidian navigation wikilinks onto files written
before link emission existed.

Idempotent — the link setters replace rather than stack, so re-running
after new summarize/synthesize work is a no-op on already-linked files.
Going forward summarize/synthesize emit links themselves; this only
catches the existing backlog. Run: `uv run chronicle backfill-links`.

Down-links are always derivable (a summary's conversation, an entry's
recorded children). Parent (up) links are written only when the parent
already exists in state — synthesize owns parent links and will stamp
any that don't exist yet when it builds that tier, so a missing parent
here is expected, not an error.
"""

from __future__ import annotations

import os
from typing import Any

from . import state as state_mod
from .links import set_full_conversation, set_parent_link, set_sources
from .paths import data_root


def _write_if_changed(rel: str, old: str, new: str) -> bool:
    if new == old:
        return False
    path = data_root() / rel
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new, encoding="utf-8")
    os.replace(tmp, path)
    return True


def _half_entry_covering(state: dict[str, Any], created_at: str) -> str | None:
    """Filename of the half/merged entry whose range contains this
    conversation's created date, if such an entry exists in state."""
    if not created_at:
        return None
    day = created_at[:10]
    for label, e in state.get("entries", {}).items():
        if e.get("tier") != "half":
            continue
        if e.get("range_start", "") <= day <= e.get("range_end", "9999"):
            ef = e.get("entry_file")
            return ef.split("/")[-1] if ef else None
    return None


def _parent_entry_of(state: dict[str, Any], child_label: str) -> str | None:
    """Filename of the entry one tier up that lists `child_label` among its
    children, if it exists."""
    for _label, e in state.get("entries", {}).items():
        if child_label in (e.get("children") or []):
            ef = e.get("entry_file")
            return ef.split("/")[-1] if ef else None
    return None


def run(args: Any) -> None:
    state = state_mod.load()
    root = data_root()

    sum_down = sum_up = 0
    for _uuid, c in state["conversations"].items():
        if c.get("deleted_at") or not c.get("summary_file"):
            continue
        rel = c["summary_file"]
        path = root / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        new = set_full_conversation(text, c["conversation_file"])
        if new != text:
            sum_down += 1
        parent = _half_entry_covering(state, c.get("created_at") or "")
        if parent:
            after = set_parent_link(new, parent)
            if after != new:
                sum_up += 1
            new = after
        if _write_if_changed(rel, text, new):
            pass

    ent_down = ent_up = 0
    for label, e in state.get("entries", {}).items():
        rel = e.get("entry_file")
        if not rel:
            continue
        path = root / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        new = text

        # Down: children. Halves point at their conversations' summaries;
        # rollups at their child entries. children list lives in state for
        # rollups; for halves we recompute the in-range summaries.
        if e.get("tier") == "half":
            from .calendar import parse_period
            try:
                _t, rs, re_ = parse_period(label)
                child_files = [
                    cc["summary_file"]
                    for _u, cc in state_mod.conversations_in_period(state, rs, re_)
                    if cc.get("summary_file")
                ]
            except Exception:
                child_files = []
        else:
            child_files = []
            for ch in e.get("children") or []:
                ce = state["entries"].get(ch)
                if ce and ce.get("entry_file"):
                    child_files.append(ce["entry_file"])

        if child_files:
            after = set_sources(new, child_files)
            if after != new:
                ent_down += 1
            new = after

        parent = _parent_entry_of(state, label)
        if parent:
            after = set_parent_link(new, parent)
            if after != new:
                ent_up += 1
            new = after

        _write_if_changed(rel, text, new)

    state_mod.save(state)
    print(
        f"Summaries: {sum_down} down-link(s) (Full conversation), "
        f"{sum_up} parent link(s) added/updated"
    )
    print(
        f"Entries:   {ent_down} Sources section(s), "
        f"{ent_up} parent link(s) added/updated"
    )
    print(
        "Done. Missing parent links are expected for tiers not yet "
        "synthesized — synthesize will stamp them when built."
    )
