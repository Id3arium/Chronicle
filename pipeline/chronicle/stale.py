"""`chronicle stale` — list stale summaries grouped by created-at date.

Optionally bounded by a period label (any form parse_period accepts —
single day, half, merged half, quarter, year). Output is grouped by
YYYY-MM-DD with a ready-to-paste `chronicle summarize --period <date>`
command per group, since rate limits make per-day runs the natural unit.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from . import state as state_mod
from .calendar import PeriodParseError, parse_period


def run(args: Any) -> None:
    state = state_mod.load()

    # Reflect ground truth: if a summary file was deleted on disk, the
    # corresponding state entry is no longer fresh.
    reset = state_mod.reconcile_summaries(state)
    if reset:
        state_mod.save(state)
        print(
            f"(reconciled: {len(reset)} summary file(s) missing on disk)\n"
        )

    if args.period:
        try:
            _tier, rs, re_ = parse_period(args.period)
        except PeriodParseError as e:
            raise SystemExit(str(e))
        rows = state_mod.conversations_in_period(state, rs, re_)
        scope_desc = f"{args.period} ({rs} → {re_})"
    else:
        rows = [(uuid, c) for uuid, c in state["conversations"].items()
                if not c.get("deleted_at")]
        scope_desc = "all tracked conversations"

    stale_rows = [(uuid, c) for uuid, c in rows if state_mod.summary_stale(c)]
    if not stale_rows:
        print(f"No stale summaries in {scope_desc}.")
        return

    by_day: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for uuid, c in stale_rows:
        day = (c.get("created_at") or "unknown")[:10]
        by_day[day].append((uuid, c))

    print(f"Stale summaries in {scope_desc}: {len(stale_rows)} across {len(by_day)} day(s)\n")
    for day in sorted(by_day):
        items = by_day[day]
        print(f"{day} — {len(items)} file(s)")
        for uuid, c in items:
            title = (c.get("title") or "(untitled)")[:70]
            print(f"  · {uuid[:8]} — \"{title}\"")
        print(f"    → uv run chronicle summarize --period {day}\n")
