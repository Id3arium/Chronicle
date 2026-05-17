"""`chronicle status` — print current pipeline state."""

from __future__ import annotations

import shutil
import subprocess

from . import state as state_mod
from .paths import inbox_dir, pending_file


def _claude_available() -> tuple[bool, str | None]:
    binary = shutil.which("claude")
    if not binary:
        return False, None
    try:
        result = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=5
        )
        return True, (result.stdout.strip() or result.stderr.strip() or binary)
    except (subprocess.TimeoutExpired, OSError):
        return True, binary


def _launchd_installed() -> bool:
    from .agent import plist_path
    return plist_path().exists()


def print_status() -> None:
    state = state_mod.load()
    convs = state["conversations"]
    entries = state["entries"]

    alive = [c for c in convs.values() if not c.get("deleted_at")]
    deleted = [c for c in convs.values() if c.get("deleted_at")]
    stale = state_mod.stale_summary_uuids(state)

    stale_entries = []
    for label, entry in entries.items():
        rs = entry.get("range_start")
        re_ = entry.get("range_end")
        if rs and re_ and state_mod.entry_stale(state, label, rs, re_):
            stale_entries.append(label)

    processed = set(
        state.get("processed_imports", []) + state.get("processed_exports", [])
    )
    unprocessed = [
        p.name
        for p in sorted(inbox_dir().glob("chronicle-export-*.json"))
        if p.name not in processed
    ]

    claude_ok, claude_info = _claude_available()

    print("Chronicle — pipeline status")
    print("=" * 32)
    print(f"Last ingest:      {state.get('last_ingest') or '(never)'}")
    print(f"Conversations:    {len(alive)} tracked ({len(deleted)} tombstoned)")
    print(f"Stale summaries:  {len(stale)}")
    print(f"Period entries:   {len(entries)} ({len(stale_entries)} stale)")
    if stale_entries:
        for label in stale_entries:
            print(f"  · {label}")
    print(f"Unprocessed in data/inbox/: {len(unprocessed)}")
    for name in unprocessed:
        print(f"  · {name}")

    pending = pending_file()
    if pending.exists():
        print(f"\npending.md present → {pending}")
    else:
        print("\npending.md: (none — pipeline idle)")

    print()
    if claude_ok:
        print(f"claude binary:    OK ({claude_info})")
    else:
        print(
            "claude binary:    NOT FOUND on $PATH. Install Claude Code "
            "(https://claude.com/claude-code) or add it to $PATH before running "
            "`chronicle summarize` / `synthesize`."
        )

    agent = _launchd_installed()
    print(f"launchd agent:    {'installed' if agent else 'not installed'}")
    if not agent:
        print("  (run `chronicle install-agent` to auto-ingest new files in data/inbox/)")
