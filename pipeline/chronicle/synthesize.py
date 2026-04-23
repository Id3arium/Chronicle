"""`chronicle synthesize --period <label>` — tiered rollup synthesis.

Tiers and what they read:

  week (2026_Apr_19-25)   reads: fresh summaries of conversations in range.
  month (2026_Apr)        reads: the 4–5 week entries covering the month.
  quarter (2026_Q2)       reads: the 3 month entries covering the quarter.
  year (2026)             reads: the 4 quarter entries.

Rules:
- Refuses if any required child input is missing or stale. Build the lower
  tier first. Token usage stays user-initiated and predictable.
- Budget: max 120k tokens of input per call (conservative against 200k
  context, leaves room for instructions + output). `chars / 4` is the
  token estimate. Single file always allowed.
- No auto-cascading. Run `chronicle synthesize --period 2026_Apr_19-25`
  four times (one per week), then `--period 2026_Apr` once, then quarter,
  then year.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import pending as pending_mod
from . import state as state_mod
from .calendar import PeriodParseError, child_tier, children_for, parse_period
from .claude_invoke import ClaudeInvocationError, run_claude
from .paths import data_root, ensure_dirs, entries_dir, instructions_dir, pending_file
from .state import now_iso

CHARS_PER_TOKEN = 4
BUDGET_TOKENS = 120_000


def _instruction_file() -> Path:
    return instructions_dir() / "synthesize.txt"


def _estimate_tokens(char_count: int) -> int:
    return char_count // CHARS_PER_TOKEN


# ────────────────── input assembly ──────────────────

def _gather_week_inputs(
    state: dict[str, Any], range_start: str, range_end: str
) -> tuple[list[dict[str, Any]], list[str], int]:
    """For a week: every non-deleted conversation's fresh summary in range.
    Returns (input_items, stale_uuids, total_chars)."""
    convs = state_mod.conversations_in_period(state, range_start, range_end)
    stale = [u for u, c in convs if state_mod.summary_stale(c)]
    items: list[dict[str, Any]] = []
    total_chars = 0
    for uuid, c in convs:
        if state_mod.summary_stale(c):
            continue  # surfaced in `stale` above; caller will refuse
        sum_rel = c.get("summary_file")
        if not sum_rel:
            continue
        sum_path = data_root() / sum_rel
        if not sum_path.exists():
            continue
        text = sum_path.read_text(encoding="utf-8")
        items.append(
            {
                "heading": f"## {c.get('title') or '(untitled)'} — {uuid}",
                "body": text,
                "chars": len(text),
            }
        )
        total_chars += len(text)
    return items, stale, total_chars


def _gather_rollup_inputs(
    state: dict[str, Any], label: str
) -> tuple[list[dict[str, Any]], list[str], int]:
    """For month/quarter/year: the child-tier entries covering the range.
    Returns (input_items, missing_or_stale_labels, total_chars)."""
    needed = children_for(label)
    entries = state.get("entries", {})
    missing: list[str] = []
    items: list[dict[str, Any]] = []
    total_chars = 0
    for child in needed:
        e = entries.get(child)
        if not e:
            missing.append(child + " (missing)")
            continue
        tier, rs, re_ = parse_period(child)
        if state_mod.entry_stale(state, child, rs, re_):
            missing.append(child + " (stale)")
            continue
        entry_path = data_root() / e["entry_file"]
        if not entry_path.exists():
            missing.append(child + " (file missing)")
            continue
        text = entry_path.read_text(encoding="utf-8")
        items.append(
            {
                "heading": f"## {child}",
                "body": text,
                "chars": len(text),
            }
        )
        total_chars += len(text)
    return items, missing, total_chars


# ────────────────── main entry ──────────────────

def run(args: Any) -> None:
    ensure_dirs()
    state = state_mod.load()

    try:
        tier, range_start, range_end = parse_period(args.period)
    except PeriodParseError as e:
        raise SystemExit(str(e))

    if tier == "week":
        items, stale_uuids, total_chars = _gather_week_inputs(
            state, range_start, range_end
        )
        if stale_uuids:
            print(
                f"{len(stale_uuids)} conversation(s) in {args.period} have stale "
                f"summaries. Run `chronicle summarize --all-stale` first, then "
                f"re-run. Stale UUIDs:"
            )
            for u in stale_uuids[:10]:
                print(f"  · {u[:8]}")
            if len(stale_uuids) > 10:
                print(f"  … and {len(stale_uuids) - 10} more")
            raise SystemExit(1)
        if not items:
            print(
                f"No non-deleted conversations in {args.period} "
                f"({range_start} → {range_end}). Nothing to synthesize."
            )
            return
    else:
        items, missing, total_chars = _gather_rollup_inputs(state, args.period)
        if missing:
            below = child_tier(tier)
            print(
                f"Cannot synthesize {args.period}. The following {below} entries "
                f"are missing or stale:"
            )
            for m in missing:
                print(f"  · {m}")
            print(
                f"\nBuild them first with `chronicle synthesize --period <label>`, "
                f"then re-run."
            )
            raise SystemExit(1)
        if not items:
            print(f"Nothing to synthesize for {args.period}.")
            return

    tokens_est = _estimate_tokens(total_chars)
    print(
        f"Synthesizing {args.period} ({tier}) · {len(items)} input(s) · "
        f"~{tokens_est:,} tokens · budget ${args.budget:.2f}"
    )
    if tokens_est > BUDGET_TOKENS and len(items) > 1:
        raise SystemExit(
            f"Input is ~{tokens_est:,} tokens, over the {BUDGET_TOKENS:,} budget. "
            f"Either roll up lower tiers first (a quarter should read 3 month "
            f"entries, not hundreds of summaries) or trim the range."
        )

    pending_text = (
        pending_file().read_text(encoding="utf-8") if pending_file().exists() else ""
    )
    input_text = (
        f"# Period to synthesize\n\n"
        f"{args.period} — {tier} — {range_start} → {range_end}\n\n"
        f"# Pending / delta context\n\n{pending_text or '(none)'}\n\n"
        f"---\n\n"
        f"# Inputs ({tier}: {len(items)} {'summary' if tier == 'week' else 'child entry'}{'ies' if len(items) != 1 else ''})\n\n"
        + "\n\n---\n\n".join(f"{it['heading']}\n\n{it['body']}" for it in items)
    )

    try:
        output = run_claude(
            _instruction_file(), input_text, max_budget_usd=args.budget
        )
    except ClaudeInvocationError as e:
        print(f"claude error: {e}")
        raise SystemExit(1)

    out_path = entries_dir() / f"{args.period}_Entry.md"
    out_path.write_text(output, encoding="utf-8")

    entry_record = {
        "tier": tier,
        "entry_file": str(out_path.relative_to(data_root())),
        "synthesized_at": now_iso(),
        "range_start": range_start,
        "range_end": range_end,
        "entry_chars": len(output),
    }
    if tier != "week":
        entry_record["children"] = children_for(args.period)
    state["entries"][args.period] = entry_record
    state_mod.save(state)
    pending_mod.write_pending(state)
    print(f"✓ {args.period} ({tier}) → {out_path.relative_to(data_root().parent)}")
