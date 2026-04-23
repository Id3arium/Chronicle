"""`chronicle synthesize --period <label> --range <start> <end>` — writes a period
entry from fresh summaries.

Refuses to run if any conversation in the period has a stale summary. User must
run `chronicle summarize` first. This keeps Claude invocations user-initiated
(tokens are scarce).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import pending as pending_mod
from . import state as state_mod
from .claude_invoke import ClaudeInvocationError, run_claude
from .paths import data_root, ensure_dirs, entries_dir, instructions_dir, pending_file
from .state import now_iso


def _instruction_file() -> Path:
    return instructions_dir() / "synthesize.txt"


def run(args: Any) -> None:
    ensure_dirs()
    state = state_mod.load()

    period = args.period
    range_start, range_end = args.range

    convs = state_mod.conversations_in_period(state, range_start, range_end)
    if not convs:
        print(
            f"No conversations in {period} ({range_start} → {range_end}). "
            f"Nothing to synthesize."
        )
        return

    stale = [uuid for uuid, c in convs if state_mod.summary_stale(c)]
    if stale:
        print(
            f"{len(stale)} conversation(s) in {period} have stale summaries:"
        )
        for uuid in stale[:10]:
            c = state["conversations"][uuid]
            print(f"  · {uuid[:8]} \"{(c.get('title') or '')[:60]}\"")
        if len(stale) > 10:
            print(f"  … and {len(stale) - 10} more")
        print(
            "\nRun `chronicle summarize --all-stale` first, then re-run synthesize. "
            "(Auto-cascading is disabled to keep token usage under your control.)"
        )
        raise SystemExit(1)

    # Build input: pending.md (why we're synthesizing) + all fresh summaries.
    summaries = []
    for uuid, c in convs:
        sum_rel = c.get("summary_file")
        if not sum_rel:
            continue  # belt-and-suspenders; summary_stale should have caught it
        sum_path = data_root() / sum_rel
        if not sum_path.exists():
            print(f"  ⚠ summary file missing for {uuid[:8]} at {sum_path} — skipping")
            continue
        text = sum_path.read_text(encoding="utf-8")
        summaries.append(f"## {c.get('title') or '(untitled)'} — {uuid}\n\n{text}\n")

    pending_text = pending_file().read_text(encoding="utf-8") if pending_file().exists() else ""

    input_text = (
        f"# Period to synthesize\n\n{period} ({range_start} → {range_end})\n\n"
        f"# Pending / delta context\n\n{pending_text or '(none)'}\n\n"
        f"---\n\n"
        f"# Fresh conversation summaries ({len(summaries)})\n\n"
        + "\n---\n\n".join(summaries)
    )

    print(f"Synthesizing {period} from {len(summaries)} summaries. Budget: ${args.budget:.2f}")

    try:
        output = run_claude(
            _instruction_file(), input_text, max_budget_usd=args.budget
        )
    except ClaudeInvocationError as e:
        print(f"claude error: {e}")
        raise SystemExit(1)

    out_path = entries_dir() / f"{period}_Entry.md"
    out_path.write_text(output, encoding="utf-8")

    state["entries"][period] = {
        "entry_file": str(out_path.relative_to(data_root())),
        "synthesized_at": now_iso(),
        "range_start": range_start,
        "range_end": range_end,
    }
    state_mod.save(state)
    pending_mod.write_pending(state)
    print(f"✓ Entry written → {out_path.relative_to(data_root().parent)}")
