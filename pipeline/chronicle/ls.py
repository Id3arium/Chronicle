"""`chronicle ls` — tabular view of conversations and their summary metrics.

Columns: date · orig words · summ words · ratio · stale? · title
Sorted by created_at. Optional --period to scope.
"""

from __future__ import annotations

from typing import Any

from . import state as state_mod
from .calendar import PeriodParseError, parse_period


def run(args: Any) -> None:
    state = state_mod.load()

    changed = state_mod.reconcile_summaries(state)
    if changed:
        state_mod.save(state)

    if args.period:
        try:
            _tier, rs, re_ = parse_period(args.period)
        except PeriodParseError as e:
            raise SystemExit(str(e))
        rows = state_mod.conversations_in_period(state, rs, re_)
        scope = f"{args.period} ({rs} → {re_})"
    else:
        rows = [
            (u, c) for u, c in state["conversations"].items()
            if not c.get("deleted_at")
        ]
        scope = "all conversations"

    # Filter by significance if requested.
    if getattr(args, "significance", None):
        sig_filter = args.significance.lower()
        valid = {"high", "medium", "med", "low"}
        if sig_filter not in valid:
            raise SystemExit(
                f"Unknown significance '{sig_filter}'. Use: high, medium (or med), low."
            )
        if sig_filter == "med":
            sig_filter = "medium"
        rows = [(u, c) for u, c in rows if c.get("significance") == sig_filter]

    rows = sorted(rows, key=lambda x: x[1].get("created_at") or "")

    if not rows:
        print(f"No conversations in {scope}.")
        return

    # Column widths
    DATE_W  = 10
    ORIG_W  = 9   # "orig wds"
    SUMM_W  = 9   # "summ wds"
    RATIO_W = 6   # "ratio"
    SIG_W   = 6   # "sig" (high/med/low/—)
    FLAG_W  = 1   # "~" stale marker
    # title fills the rest; we'll cap at terminal width if available
    try:
        import shutil as _sh
        term_w = _sh.get_terminal_size((120, 40)).columns
    except Exception:
        term_w = 120
    TITLE_W = max(20, term_w - DATE_W - ORIG_W - SUMM_W - RATIO_W - SIG_W - FLAG_W - 8)

    header = (
        f"{'date':<{DATE_W}}  "
        f"{'orig wds':>{ORIG_W}}  "
        f"{'summ wds':>{SUMM_W}}  "
        f"{'ratio':>{RATIO_W}}  "
        f"{'sig':<{SIG_W}}  "
        f"{'title':<{TITLE_W}}"
    )
    sep = "-" * len(header)
    print(f"\n{scope} — {len(rows)} conversation(s)\n")
    print(header)
    print(sep)

    SIG_ABBR = {"high": "high", "medium": "med", "low": "low"}

    for uuid, c in rows:
        date   = (c.get("created_at") or "")[:10]
        orig   = c.get("original_words") or 0
        summ   = c.get("summary_words") or 0
        ratio  = c.get("compression_ratio")
        sig    = SIG_ABBR.get(c.get("significance") or "", "—")
        stale  = "~" if state_mod.summary_stale(c) else " "
        title  = (c.get("title") or "(untitled)")

        ratio_s = f"{ratio:.3f}" if ratio is not None else "  n/a"
        orig_s  = f"{orig:,}"    if orig  else "  —"
        summ_s  = f"{summ:,}"    if summ  else "  —"

        title_display = title if len(title) <= TITLE_W - 2 else title[:TITLE_W - 5] + "…"

        print(
            f"{date:<{DATE_W}}  "
            f"{orig_s:>{ORIG_W}}  "
            f"{summ_s:>{SUMM_W}}  "
            f"{ratio_s:>{RATIO_W}}  "
            f"{sig:<{SIG_W}}  "
            f"{stale}{title_display}"
        )

    # Footer summary
    total_orig = sum(c.get("original_words") or 0 for _, c in rows)
    total_summ = sum(c.get("summary_words") or 0 for _, c in rows)
    agg_ratio  = round(total_summ / total_orig, 3) if total_orig else None
    n_stale    = sum(1 for _, c in rows if state_mod.summary_stale(c))
    n_fresh    = len(rows) - n_stale

    print(sep)
    ratio_s = f"{agg_ratio:.3f}" if agg_ratio is not None else "n/a"
    print(
        f"{'totals':<{DATE_W}}  "
        f"{total_orig:>{ORIG_W},}  "
        f"{total_summ:>{SUMM_W},}  "
        f"{ratio_s:>{RATIO_W}}  "
        f"{n_fresh} fresh · {n_stale} stale"
    )
    print()
