"""`chronicle summarize` — the only command that calls Claude (besides synthesize).

Targets are chosen explicitly by the user:
- --uuid X        : one conversation
- --all-stale     : every conversation where summary_stale() is True (default)
- --period YYYY-MM: every non-deleted conversation whose created_at falls in
                    that month

For each target: read the per-conversation JSON + pending.md (as context),
invoke `claude -p` with files/summarize.txt, capture stdout, write
data/summaries/YYYY-MM/{uuid}.md, update state.summarized_at. Errors print
and continue.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import pending as pending_mod
from . import state as state_mod
from .claude_invoke import (
    ClaudeInvocationError,
    ClaudeNotFoundError,
    run_claude,
)
from .paths import (
    data_root,
    ensure_dirs,
    instructions_dir,
    pending_file,
    summaries_dir,
)
from .state import now_iso


def _instruction_file() -> Path:
    return instructions_dir() / "summarize.txt"


def _read_pending_context() -> str:
    p = pending_file()
    if p.exists():
        return p.read_text(encoding="utf-8")
    return "(no pending.md — nothing flagged by recent ingest)"


def _select_targets(state: dict[str, Any], args: Any) -> list[str]:
    convs = state["conversations"]
    if args.uuid:
        if args.uuid not in convs:
            raise SystemExit(
                f"UUID {args.uuid} not in state.json. Run `chronicle ingest` first "
                f"or double-check the UUID."
            )
        return [args.uuid]
    if args.period:
        period = args.period  # "YYYY-MM"
        # month bounds
        rs = f"{period}-01"
        # naive last-day: ingest keyed on created_at YYYY-MM, so string prefix is enough.
        return [
            uuid
            for uuid, c in convs.items()
            if not c.get("deleted_at")
            and (c.get("created_at") or "")[:7] == period
            and state_mod.summary_stale(c)
        ]
    # default: all stale
    return state_mod.stale_summary_uuids(state)


def summarize_one(
    uuid: str,
    state: dict[str, Any],
    *,
    pending_context: str,
    budget_usd: float,
) -> bool:
    """Returns True on success, False on failure. Mutates state on success."""
    conv_meta = state["conversations"].get(uuid)
    if not conv_meta:
        print(f"  ✗ {uuid[:8]} — not in state (skipping)")
        return False
    conv_path = data_root() / conv_meta["conversation_file"]
    if not conv_path.exists():
        print(
            f"  ✗ {uuid[:8]} — conversation file missing at {conv_path}. "
            f"Re-ingest the source export."
        )
        return False

    conv_text = conv_path.read_text(encoding="utf-8")
    input_text = (
        f"# Pending work context\n\n{pending_context}\n\n"
        f"---\n\n"
        f"# Conversation JSON\n\n{conv_text}\n"
    )

    try:
        output = run_claude(
            _instruction_file(),
            input_text,
            max_budget_usd=budget_usd,
        )
    except ClaudeInvocationError as e:
        print(f"  ✗ {uuid[:8]} — claude error: {e}")
        return False

    # Write summary. Month derived from created_at so summaries mirror
    # conversations/ layout.
    month = (conv_meta.get("created_at") or "unknown")[:7]
    out_dir = summaries_dir() / month
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{uuid}.md"
    out_path.write_text(output, encoding="utf-8")

    rel = str(out_path.relative_to(data_root()))
    conv_meta["summary_file"] = rel
    conv_meta["summarized_at"] = now_iso()

    title = conv_meta.get("title") or "(untitled)"
    print(f"  ✓ {uuid[:8]} — \"{title[:60]}\" → {rel}")
    return True


def run(args: Any) -> None:
    ensure_dirs()
    state = state_mod.load()

    try:
        targets = _select_targets(state, args)
    except SystemExit:
        raise

    if not targets:
        print("Nothing to summarize. All summaries are fresh.")
        return

    # Fail fast if claude isn't installed before we start processing.
    from shutil import which
    if not which("claude"):
        raise SystemExit(
            "`claude` binary not found on $PATH. Install Claude Code "
            "(https://claude.com/claude-code) and ensure `claude --version` works, "
            "then re-run."
        )

    pending_context = _read_pending_context()
    print(f"Summarizing {len(targets)} conversation(s). Budget per call: ${args.budget:.2f}")
    succeeded = 0
    failed = 0
    for uuid in targets:
        if summarize_one(
            uuid, state, pending_context=pending_context, budget_usd=args.budget
        ):
            succeeded += 1
            # Save after each success so a crash doesn't lose progress.
            state_mod.save(state)
        else:
            failed += 1

    # One final rewrite of pending.md so processed UUIDs fall off.
    pending_mod.write_pending(state)
    print(f"\nDone. {succeeded} ok, {failed} failed.")
    if failed:
        print("Re-run `chronicle summarize --uuid <uuid>` to retry specific failures.")
