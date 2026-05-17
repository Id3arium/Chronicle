"""`chronicle synthesize --period <label>` — tiered rollup synthesis.

Tiers and what they read:

  half  (2026_Apr_H1)  reads: fresh summaries of conversations in range.
  half  (2026_Apr_H2)  reads: fresh summaries of conversations in range.
  month (2026_Apr)     reads: the 2 half entries (H1 + H2).
  quarter (2026_Q2)    reads: the 3 month entries.
  year (2026)          reads: the 4 quarter entries.

Rules:
- Refuses if any required child input is missing or stale. Build the lower
  tier first. Token usage stays user-initiated and predictable.
- Budget: max 120k tokens of input per call (conservative against 200k
  context, leaves room for instructions + output). `chars / 4` is the
  token estimate. Single file always allowed.
- No auto-cascading. Run `chronicle synthesize --period 2026_Apr_H1`,
  then `--period 2026_Apr_H2`, then `--period 2026_Apr`, then quarter,
  then year.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import pending as pending_mod
from . import state as state_mod
from .calendar import (
    MONTH_ABBR,
    PeriodParseError,
    canonical_merged_label,
    child_tier,
    children_for,
    parse_period,
)
from .claude_invoke import ClaudeInvocationError, run_claude
from .metrics import entry_body, measure_text, parse_frontmatter
from .paths import (
    data_root,
    ensure_dirs,
    entries_dir,
    glossary_file,
    instructions_dir,
    pending_file,
)
from .state import now_iso

CHARS_PER_TOKEN = 4
BUDGET_TOKENS = 120_000

# Below this many non-deleted conversations in a month, halves auto-merge
# into a single 2026_Apr_H1-H2 entry. Two thin halves rarely compress well;
# one merged entry reads better and the quarter rollup costs less.
SPARSE_MONTH_THRESHOLD = 10


def _instruction_file() -> Path:
    return instructions_dir() / "synthesize.txt"


def _estimate_tokens(char_count: int) -> int:
    return char_count // CHARS_PER_TOKEN


# ────────────────── label classification + auto-merge ──────────────────

def _half_label_kind(label: str) -> str | None:
    """Classify a half-tier label. Accepts both abbr and numeric month forms.
    Returns one of:
      "half_h1"  — '2026_Apr_H1' / '2026_04_H1'
      "half_h2"  — '2026_Apr_H2' / '2026_04_H2'
      "merged"   — '2026_Apr_H1-H2' / '2026_Apr' / '2026_04_H1-H2' / '2026_04'
      None       — not a half-tier label
    """
    import re as _re
    MON = r"(?:[A-Z][a-z]{2}|0[1-9]|1[0-2])"
    if _re.fullmatch(rf"\d{{4}}_{MON}_H1", label):
        return "half_h1"
    if _re.fullmatch(rf"\d{{4}}_{MON}_H2", label):
        return "half_h2"
    if _re.fullmatch(rf"\d{{4}}_{MON}_H1-H2", label):
        return "merged"
    if _re.fullmatch(rf"\d{{4}}_{MON}", label):
        return "merged"
    return None


def _month_year_abbr(label: str) -> tuple[int, str]:
    """Extract (year, month_abbr) from any half-tier label.
    Numeric months are converted to their abbr equivalent."""
    import re as _re
    m = _re.match(r"(\d{4})_([A-Z][a-z]{2})", label)
    if m:
        return int(m.group(1)), m.group(2)
    m = _re.match(r"(\d{4})_(0[1-9]|1[0-2])", label)
    if m:
        return int(m.group(1)), MONTH_ABBR[int(m.group(2)) - 1]
    raise PeriodParseError(f"Cannot extract year/month from '{label}'")


def _month_conversation_count(state: dict[str, Any], year: int, abbr: str) -> int:
    """Count non-deleted conversations whose created_at falls in this month."""
    # Reuse parse_period via the merged label which spans the full month.
    _t, rs, re_ = parse_period(f"{year}_{abbr}")
    return len(state_mod.conversations_in_period(state, rs, re_))


def _resolve_half_label(
    state: dict[str, Any], requested: str
) -> tuple[str, str | None]:
    """Apply auto-merge rules. Given the user-requested half label, return
    (canonical_label, info_message_for_user_or_None).

    Rules:
    - If requested is H1 or H2 and the month has < threshold conversations,
      redirect to the canonical merged label (year_Mon_H1-H2). Print why.
    - If requested is merged (H1-H2 or month alias) and the month has
      >= threshold, refuse — tell the user to run halves separately.
    - Otherwise pass through unchanged. The 2026_Apr alias normalizes to
      2026_Apr_H1-H2 so state and filenames are consistent.
    """
    kind = _half_label_kind(requested)
    if kind is None:
        return requested, None  # non-half label, leave alone

    year, abbr = _month_year_abbr(requested)
    count = _month_conversation_count(state, year, abbr)
    canonical_merged = canonical_merged_label(year, abbr_to_int(abbr))

    if kind in ("half_h1", "half_h2"):
        if count < SPARSE_MONTH_THRESHOLD:
            msg = (
                f"{year}_{abbr} has {count} conversations (< {SPARSE_MONTH_THRESHOLD}). "
                f"Auto-merging halves into a single entry: {canonical_merged}."
            )
            return canonical_merged, msg
        return requested, None

    # kind == "merged" — either explicit H1-H2 or the bare month alias
    if count >= SPARSE_MONTH_THRESHOLD:
        # Refuse the merged form when the month has enough material for halves.
        raise SystemExit(
            f"{year}_{abbr} has {count} conversations (>= {SPARSE_MONTH_THRESHOLD}). "
            f"Run the halves separately:\n"
            f"  chronicle synthesize --period {year}_{abbr}_H1\n"
            f"  chronicle synthesize --period {year}_{abbr}_H2\n"
            f"The merged form is for sparse months only."
        )
    # Sparse + explicitly merged: normalize the alias 2026_Apr to 2026_Apr_H1-H2.
    if requested != canonical_merged:
        return canonical_merged, (
            f"Normalizing '{requested}' to canonical '{canonical_merged}'."
        )
    return canonical_merged, None


def abbr_to_int(abbr: str) -> int:
    from .calendar import ABBR_TO_MONTH
    return ABBR_TO_MONTH[abbr]


def _build_entry_metrics(
    items: list[dict[str, Any]], output: str
) -> dict[str, Any]:
    """Compute the 5 entry-level metrics from the source items + Claude's
    output. Returned dict is keyed for direct frontmatter injection.

    `aggregate_source_compression_ratio` = total_summary / total_original.
    This weights by actual size — a true "how compressed is this whole
    pile" number. Averaging per-conversation ratios would weight each
    conversation equally regardless of size, which inflates the number
    when a small conversation has a high ratio.
    """
    total_orig = sum(i.get("source_metrics", {}).get("original_words", 0) for i in items)
    total_sum = sum(i.get("source_metrics", {}).get("summary_words", 0) for i in items)
    aggregate_ratio = (
        round(total_sum / total_orig, 4) if total_orig else 0.0
    )
    body = entry_body(output)
    entry_words = measure_text(body)["words"]
    entry_ratio = (
        round(entry_words / total_sum, 4) if total_sum else 0.0
    )
    return {
        "total_source_conversation_words": total_orig,
        "total_source_summary_words": total_sum,
        "aggregate_source_compression_ratio": aggregate_ratio,
        "entry_words": entry_words,
        "entry_compression_ratio": entry_ratio,
    }


def _inject_entry_metrics(output: str, metrics: dict[str, Any]) -> str:
    """Merge entry-level metrics into the frontmatter. Entries usually open
    with `# ... Entry` (no frontmatter), so split_frontmatter returns an
    empty dict and we build a fresh block. If Claude did emit frontmatter,
    its keys are preserved in order and metrics merged in. Parse-and-
    reserialize, never splice — a body `---` can't be misread as a fence."""
    from .metrics import render_with_frontmatter, split_frontmatter

    fields, body = split_frontmatter(output)
    fields.update(metrics)
    return render_with_frontmatter(fields, body)


# ────────────────── input assembly ──────────────────

def _gather_half_inputs(
    state: dict[str, Any], range_start: str, range_end: str
) -> tuple[list[dict[str, Any]], list[str], int]:
    """For a half: every non-deleted conversation's fresh summary in range.
    Returns (input_items, stale_uuids, total_chars).

    Each item carries `source_metrics`: original_words, summary_words,
    compression_ratio. Pulled from state if present, else from summary
    frontmatter, so entry-level rollup metrics are cheap and accurate."""
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
        fm = parse_frontmatter(text)
        # State is the preferred source; fall back to frontmatter for legacy
        # files written before state tracked these.
        ow = c.get("original_words") or _to_int(fm.get("original_words"))
        sw = c.get("summary_words") or _to_int(fm.get("summary_words"))
        cr = c.get("compression_ratio")
        if cr is None:
            cr = _to_float(fm.get("compression_ratio"))
        items.append(
            {
                "heading": f"## {c.get('title') or '(untitled)'} — {uuid}",
                "body": text,
                "chars": len(text),
                "source_metrics": {
                    "original_words": ow or 0,
                    "summary_words": sw or 0,
                    "compression_ratio": cr or 0.0,
                },
            }
        )
        total_chars += len(text)
    return items, stale, total_chars


def _to_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _to_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _resolve_children_with_merges(
    state: dict[str, Any], needed: list[str]
) -> list[str]:
    """Walk the half labels in `needed` and substitute merged H1-H2 entries
    where they exist. If 2026_Apr_H1 and 2026_Apr_H2 are both in `needed`
    and 2026_Apr_H1-H2 exists in state.entries, both are replaced by the
    single merged label. Order is preserved (the merged label takes the
    position of the first half it replaces).
    """
    entries = state.get("entries", {})
    out: list[str] = []
    skip_next_for_month: set[str] = set()  # "2026_Apr" markers
    for child in needed:
        # Only halves can be merged.
        if "_H1" in child or "_H2" in child:
            # Extract the month key, e.g. "2026_Apr"
            month_key = child.split("_H")[0]
            if month_key in skip_next_for_month:
                continue  # already handled by the merged entry
            merged_label = f"{month_key}_H1-H2"
            if merged_label in entries:
                out.append(merged_label)
                skip_next_for_month.add(month_key)
                continue
        out.append(child)
    return out


def _gather_rollup_inputs(
    state: dict[str, Any], label: str
) -> tuple[list[dict[str, Any]], list[str], int]:
    """For quarter/year: the child-tier entries covering the range.

    Quarterly children are the 6 halves of its 3 months. If a month was
    sparse and synthesized as a merged H1-H2 entry, it stands in for both
    of that month's halves — quarter sees 5 (or fewer) inputs cleanly,
    not the same merged file twice.

    Returns (input_items, missing_or_stale_labels, total_chars)."""
    needed = _resolve_children_with_merges(state, children_for(label))
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
        fm = parse_frontmatter(text)
        # Rollup metrics: pull totals from the child's frontmatter so this
        # entry's metrics roll cleanly. Falls back to 0 if the child predates
        # the metrics injection (older entries).
        items.append(
            {
                "heading": f"## {child}",
                "body": text,
                "chars": len(text),
                "source_metrics": {
                    # At a rollup tier, the "source" for THIS entry is its
                    # child entries. So:
                    #   original_words = the child's transitive conversation
                    #                    word total (carries through tiers)
                    #   summary_words  = the child entry's own word count
                    #                    (it IS this tier's "summary" input)
                    #   compression_ratio = the child's own entry compression
                    "original_words": _to_int(fm.get("total_source_conversation_words")) or 0,
                    "summary_words": _to_int(fm.get("entry_words")) or 0,
                    "compression_ratio": _to_float(fm.get("entry_compression_ratio")) or 0.0,
                },
            }
        )
        total_chars += len(text)
    return items, missing, total_chars


# ────────────────── main entry ──────────────────

def run(args: Any) -> None:
    ensure_dirs()
    state = state_mod.load()

    from .calendar import canonical_label
    try:
        tier, _rs0, _re0 = parse_period(args.period)
    except PeriodParseError as e:
        raise SystemExit(str(e))

    # Canonicalize: 2026-03-h1 → 2026_Mar_H1, etc.
    args.period = canonical_label(args.period)

    if tier == "day":
        raise SystemExit(
            f"Day labels (e.g. {args.period}) are summarize-only — synthesis "
            f"starts at the half tier. Try `chronicle synthesize --period "
            f"{args.period[:7].replace('-', '_')}_H1` (or _H2)."
        )

    # Auto-merge / normalize sparse-month half labels. If the user typed
    # 2026_Apr_H1 but April only has 4 conversations, this redirects to
    # 2026_Apr_H1-H2. Also normalizes the 2026_Apr alias to its canonical
    # merged form. For non-half tiers, returns the label unchanged.
    if tier == "half":
        canonical, redirect_msg = _resolve_half_label(state, args.period)
        if redirect_msg:
            print(redirect_msg)
        args.period = canonical

    # Re-parse after possible redirect so range_start/range_end match the
    # canonical label, not what the user typed.
    tier, range_start, range_end = parse_period(args.period)

    skip_prompts = getattr(args, "yes", False)

    # The period is partial if today falls before its end date — more
    # conversations can still land in range. This drives three things: the
    # CLI confirmation below, an `is_partial: true` frontmatter flag, and a
    # prose instruction telling Claude to frame the entry as provisional.
    from datetime import date as _date
    today = _date.today().isoformat()
    is_partial = today < range_end
    if is_partial and not skip_prompts:
        print(
            f"⚠ Period {args.period} ends on {range_end}, but today is {today}.\n"
            f"  Conversations may still be added before the period closes.\n"
            f"  Synthesizing now means you'll likely need to re-synthesize later.\n"
            f"  The entry will be marked is_partial: true (covers through {today})."
        )
        try:
            answer = input("  Continue anyway? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            print("Aborted.")
            return

    # Idempotency: if a fresh entry already exists at this label, no work.
    existing_entry = state.get("entries", {}).get(args.period)
    if existing_entry and not state_mod.entry_stale(
        state, args.period, range_start, range_end
    ):
        ef = data_root() / existing_entry["entry_file"]
        if ef.exists():
            print(
                f"✓ {args.period} entry already fresh — no work needed.\n"
                f"  {ef.relative_to(data_root().parent)}\n"
                f"  (delete the entry's `synthesized_at` in state.json or wait for "
                f"a conversation update to mark it stale.)"
            )
            return

    if tier == "half":
        items, stale_uuids, total_chars = _gather_half_inputs(
            state, range_start, range_end
        )
        if stale_uuids:
            print(
                f"⚠ {len(stale_uuids)} conversation(s) in {args.period} have stale "
                f"summaries:"
            )
            for u in stale_uuids[:10]:
                c = state["conversations"].get(u, {})
                t = (c.get("title") or "(untitled)")[:50]
                print(f"  · {u[:8]} — {t}")
            if len(stale_uuids) > 10:
                print(f"  … and {len(stale_uuids) - 10} more")
            print(f"\n  Fix with: chronicle sum -d {range_start} {range_end}")
            if not skip_prompts:
                try:
                    answer = input("  Continue without them? [y/N] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    answer = ""
                if answer not in ("y", "yes"):
                    print("Aborted. Summarize first, then re-run synthesize.")
                    return
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
                # `m` looks like "2025_May_H1 (missing)" — extract the label.
                label_only = m.split(" ", 1)[0]
                print(f"  · {m}")
                print(f"      chronicle synthesize --period {label_only}")
            print(
                "\nBuild them in the order shown, then re-run. (Sparse months "
                "auto-merge into a single YYYY_Mon_H1-H2 entry — running either "
                "half label will produce it.)"
            )
            raise SystemExit(1)
        if not items:
            print(f"Nothing to synthesize for {args.period}.")
            return

    tokens_est = _estimate_tokens(total_chars)
    print(
        f"Synthesizing {args.period} ({tier}) · {len(items)} input(s) · "
        f"~{tokens_est:,} tokens"
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
    glossary_text = (
        glossary_file().read_text(encoding="utf-8")
        if glossary_file().exists()
        else "(no glossary file present)"
    )
    partial_note = (
        (
            f"# ⚠ PARTIAL PERIOD\n\n"
            f"This {tier} is being synthesized BEFORE it has ended. It covers "
            f"{range_start} through {range_end}, but only conversations up to "
            f"{today} exist so far. Write the entry as explicitly provisional: "
            f"state up front that it is a partial {tier} covering through "
            f"{today}, and frame conclusions as in-progress rather than final. "
            f"This entry will be regenerated once the period closes.\n\n"
            f"---\n\n"
        )
        if is_partial
        else ""
    )
    input_text = (
        f"# Glossary (project/term reference — use when matching, leave verbatim otherwise)\n\n"
        f"{glossary_text}\n\n"
        f"---\n\n"
        f"{partial_note}"
        f"# Period to synthesize\n\n"
        f"{args.period} — {tier} — {range_start} → {range_end}\n\n"
        f"# Pending / delta context\n\n{pending_text or '(none)'}\n\n"
        f"---\n\n"
        f"# Inputs ({tier}: {len(items)} {'summary' if tier == 'half' else 'child entry'}{'ies' if len(items) != 1 else ''})\n\n"
        + "\n\n---\n\n".join(f"{it['heading']}\n\n{it['body']}" for it in items)
    )

    model = getattr(args, "model", None) or "claude-opus-4-7"
    print(f"  Model: {model}")
    try:
        output = run_claude(
            _instruction_file(), input_text,
            model=model,
        )
    except ClaudeInvocationError as e:
        print(f"claude error: {e}")
        raise SystemExit(1)

    # Append bibliography: list of source conversations/entries that fed
    # this synthesis. Makes it possible to trace claims back to sources.
    bib_lines = ["\n\n---\n\n## Sources\n"]
    if tier == "half":
        for it in items:
            # heading is "## Title — uuid"
            heading = it["heading"].lstrip("# ").strip()
            sm = it.get("source_metrics", {})
            sig = ""
            # Try to pull significance from the summary frontmatter
            fm = parse_frontmatter(it["body"])
            if fm.get("significance"):
                sig = f" [{fm['significance']}]"
            bib_lines.append(f"- {heading}{sig}")
    else:
        for it in items:
            heading = it["heading"].lstrip("# ").strip()
            bib_lines.append(f"- {heading}")
    output = output.rstrip() + "\n".join(bib_lines) + "\n"

    # Compute and inject the frontmatter BEFORE writing. Two groups:
    # - identity/period metadata (period, tier, range, created_at, input count)
    # - length metrics (the 5 word/ratio fields)
    # Identity comes first so the YAML reads naturally top-down.
    entry_metrics = _build_entry_metrics(items, output)
    identity = {
        "period": args.period,
        "tier": tier,
        "range_start": range_start,
        "range_end": range_end,
        "synthesized_at": now_iso(),
        "model": model,
        "input_count": len(items),
    }
    if is_partial:
        # Only emitted when true. A complete entry simply omits the key, so
        # `fm.get("is_partial")` is falsy and old entries stay valid.
        identity["is_partial"] = "true"
        identity["partial_through"] = today
    output = _inject_entry_metrics(output, {**identity, **entry_metrics})

    out_path = entries_dir() / f"{args.period}_Entry.md"
    # Atomic write: a partially-written entry would poison subsequent rollups
    # (a stale month read of a half-truncated H1 entry, etc).
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_text(output, encoding="utf-8")
    os.replace(tmp_path, out_path)

    entry_record = {
        "tier": tier,
        "entry_file": str(out_path.relative_to(data_root())),
        "synthesized_at": now_iso(),
        "range_start": range_start,
        "range_end": range_end,
        "entry_chars": len(output),
        "is_partial": is_partial,
        "model": model,
        **entry_metrics,
    }
    if tier != "half":
        entry_record["children"] = children_for(args.period)
    state["entries"][args.period] = entry_record
    state_mod.save(state)
    pending_mod.write_pending(state)
    print(f"✓ {args.period} ({tier}) → {out_path.relative_to(data_root().parent)}")
