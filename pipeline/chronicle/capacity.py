"""`chronicle capacity <period-label>` — preview what a synthesize call would send.

Prints the tier, the inputs it would pack, char counts, estimated tokens,
and whether any required child is missing or stale. No Claude invocation.
"""

from __future__ import annotations

from typing import Any

from . import state as state_mod
from .calendar import PeriodParseError, parse_period
from .synthesize import (
    BUDGET_TOKENS,
    _gather_rollup_inputs,
    _gather_week_inputs,
    _estimate_tokens,
)


def run(args: Any) -> None:
    state = state_mod.load()
    try:
        tier, rs, re_ = parse_period(args.period)
    except PeriodParseError as e:
        raise SystemExit(str(e))

    print(f"Period:  {args.period}")
    print(f"Tier:    {tier}")
    print(f"Range:   {rs} → {re_}")

    if tier == "week":
        items, stale, total_chars = _gather_week_inputs(state, rs, re_)
        print(f"Inputs:  {len(items)} fresh summary/ies")
        if stale:
            print(
                f"  ⚠ {len(stale)} conversation(s) in range have stale summaries — "
                f"run `chronicle summarize` first."
            )
    else:
        items, missing, total_chars = _gather_rollup_inputs(state, args.period)
        print(f"Inputs:  {len(items)} child entry/ies")
        if missing:
            print("  ⚠ Missing or stale children:")
            for m in missing:
                print(f"    · {m}")

    tokens = _estimate_tokens(total_chars)
    pct = (tokens / BUDGET_TOKENS) * 100 if BUDGET_TOKENS else 0
    print(f"Chars:   {total_chars:,}")
    print(f"Tokens:  ~{tokens:,} (budget {BUDGET_TOKENS:,}, {pct:.1f}%)")
    if tokens > BUDGET_TOKENS and len(items) > 1:
        print(
            "  ✗ Over budget. Roll up a lower tier first, or narrow the range."
        )
    elif tokens > BUDGET_TOKENS * 0.8:
        print("  ⚠ Within 20% of budget — synthesis may be cramped.")
    else:
        print("  ✓ Fits.")
