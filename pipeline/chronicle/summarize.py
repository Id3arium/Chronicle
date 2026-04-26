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

import os
from pathlib import Path
from typing import Any

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import pending as pending_mod
from . import state as state_mod
from .claude_invoke import (
    ClaudeInvocationError,
    ClaudeNotFoundError,
    run_claude,
)
from .metrics import compression_ratio, measure_text
from .paths import (
    data_root,
    ensure_dirs,
    instructions_dir,
    pending_file,
    stem_for,
    summaries_dir,
)
from .state import now_iso


def _instruction_file() -> Path:
    return instructions_dir() / "summarize.txt"


def _inject_metrics(
    output: str,
    orig_words: int,
    summary_metrics: dict[str, int],
    ratio: float,
) -> str:
    """Append length metrics inside the frontmatter block.

    Looks for the first `---` block at the top. If found, inserts metric lines
    just before its closing `---`. If not found, prepends a fresh block.
    """
    metric_lines = (
        f"original_words: {orig_words}\n"
        f"summary_words: {summary_metrics['words']}\n"
        f"compression_ratio: {ratio}\n"
    )
    text = output.lstrip()
    if text.startswith("---\n"):
        # Find the closing fence.
        rest = text[4:]
        end = rest.find("\n---")
        if end != -1:
            head = text[: 4 + end + 1]  # up through the newline before the closing ---
            tail = text[4 + end + 1 :]   # the closing --- line and everything after
            return head + metric_lines + tail
    # No frontmatter found — prepend a minimal one.
    return f"---\n{metric_lines}---\n\n{output}"


def _read_pending_context() -> str:
    p = pending_file()
    if p.exists():
        return p.read_text(encoding="utf-8")
    return "(no pending.md — nothing flagged by recent ingest)"


def _select_targets(state: dict[str, Any], args: Any) -> tuple[list[str], int, str]:
    """Return (stale_uuids, total_in_scope, scope_desc).

    total_in_scope counts non-deleted conversations matching the filter,
    regardless of staleness — lets the caller distinguish "no conversations
    here" from "all caught up". scope_desc is a human label for messages.
    """
    convs = state["conversations"]
    # -pn / --period-now is sugar for `-p <date> now`. Normalize early so the
    # rest of this function only has to handle args.period.
    if getattr(args, "period_now", None):
        args.period = [args.period_now, "now"]
    if args.uuid:
        if args.uuid not in convs:
            raise SystemExit(
                f"UUID {args.uuid} not in state.json. Run `chronicle ingest` first "
                f"or double-check the UUID."
            )
        c = convs[args.uuid]
        in_scope = 0 if c.get("deleted_at") else 1
        stale = [args.uuid] if state_mod.summary_stale(c) else []
        return stale, in_scope, f"UUID {args.uuid[:8]}"
    if args.period:
        from .calendar import PeriodParseError, parse_period
        labels = args.period if isinstance(args.period, list) else [args.period]
        if len(labels) == 1:
            try:
                if labels[0].lower() == "now":
                    from datetime import date as _date
                    today = _date.today().isoformat()
                    _tier, rs, re_ = "day", today, today
                else:
                    _tier, rs, re_ = parse_period(labels[0])
            except PeriodParseError as e:
                raise SystemExit(str(e))
        elif len(labels) == 2:
            # Two-arg form: inclusive range. Both must be single-day labels;
            # mixing tiers (e.g. day → quarter) is ambiguous and rejected.
            # The literal "now" is allowed in either slot to mean today's
            # date, so `-p 2026-03-20 now` covers everything from the 20th
            # forward. (Past `now` works too, since fresh files just skip.)
            from datetime import date as _date
            today_iso = _date.today().isoformat()

            def _resolve(lbl: str) -> tuple[str, str, str]:
                if lbl.lower() == "now":
                    return ("day", today_iso, today_iso)
                return parse_period(lbl)

            try:
                t1, s1, _e1 = _resolve(labels[0])
                t2, _s2, e2 = _resolve(labels[1])
            except PeriodParseError as e:
                raise SystemExit(str(e))
            if t1 != "day" or t2 != "day":
                raise SystemExit(
                    f"Two-argument --period requires single-day labels (YYYY-MM-DD or 'now'), "
                    f"got '{labels[0]}' ({t1}) and '{labels[1]}' ({t2}). "
                    f"For wider spans use a single label like 2026_Mar_H2."
                )
            if s1 > e2:
                raise SystemExit(
                    f"Range start {labels[0]} is after end {labels[1]}. Swap them."
                )
            rs, re_ = s1, e2
        else:
            raise SystemExit(
                f"--period takes 1 or 2 values, got {len(labels)}: {labels}"
            )
        rows = state_mod.conversations_in_period(state, rs, re_)
        stale = [uuid for uuid, c in rows if state_mod.summary_stale(c)]
        scope = (
            f"{labels[0]} → {labels[1]}" if len(labels) == 2 else labels[0]
        )
        return stale, len(rows), f"period {scope} ({rs} → {re_})"
    # default: all stale across every tracked conversation
    alive = [(u, c) for u, c in convs.items() if not c.get("deleted_at")]
    stale = [u for u, c in alive if state_mod.summary_stale(c)]
    return stale, len(alive), "all tracked conversations"


def summarize_one(
    uuid: str,
    state: dict[str, Any],
    *,
    pending_context: str,
    model: str | None = None,
) -> bool:
    """Returns True on success, False on failure. Mutates state on success."""
    conv_meta = state["conversations"].get(uuid)
    if not conv_meta:
        print(f"  ✗ {uuid[:8]} — not in state (skipping)", flush=True)
        return False
    conv_path = data_root() / conv_meta["conversation_file"]
    if not conv_path.exists():
        print(
            f"  ✗ {uuid[:8]} — conversation file missing at {conv_path}. "
            f"Re-ingest the source export.",
            flush=True,
        )
        return False

    conv_text = conv_path.read_text(encoding="utf-8")
    orig_chars = conv_meta.get("original_chars") or 0
    orig_words = conv_meta.get("original_words") or 0
    orig_tokens = conv_meta.get("original_tokens_est") or 0
    metrics_block = (
        f"# Original conversation metrics (prose only, excludes JSON wrapper)\n\n"
        f"original_chars: {orig_chars}\n"
        f"original_words: {orig_words}\n"
        f"original_tokens_est: {orig_tokens}\n\n"
        f"After you write your summary, the wrapper computes summary_chars / "
        f"summary_words / summary_tokens_est / compression_ratio and appends "
        f"them to the frontmatter automatically. You do NOT need to include "
        f"them yourself — leave length metrics out of your output.\n"
    )
    input_text = (
        f"# Pending work context\n\n{pending_context}\n\n"
        f"---\n\n"
        f"{metrics_block}\n"
        f"---\n\n"
        f"# Conversation JSON\n\n{conv_text}\n"
    )

    try:
        output = run_claude(
            _instruction_file(),
            input_text,
            model=model,
        )
    except ClaudeInvocationError as e:
        print(f"  ✗ {uuid[:8]} — claude error: {e}", flush=True)
        return False

    # Write summary. Month derived from created_at so summaries mirror
    # conversations/ layout.
    month = (conv_meta.get("created_at") or "unknown")[:7]
    out_dir = summaries_dir() / month
    out_dir.mkdir(parents=True, exist_ok=True)
    # Reuse existing summary filename if present (survives title edits).
    existing_sum = conv_meta.get("summary_file")
    if existing_sum and not existing_sum.startswith("summaries/deleted/"):
        out_path = data_root() / existing_sum
        out_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        out_path = out_dir / f"{stem_for(uuid, conv_meta.get('title'), conv_meta.get('created_at'))}.md"
    # Inject length metrics into the frontmatter. The summary's own prose is
    # the apples-to-apples count; we measure the whole stdout so it covers
    # frontmatter + body, which is fine for ratio purposes.
    summary_metrics = measure_text(output)
    ratio = compression_ratio(summary_metrics["chars"], orig_chars)
    output = _inject_metrics(output, orig_words, summary_metrics, ratio)
    # Atomic write: never leave a half-finished summary on disk if something
    # crashes between bytes. Write to a sibling .tmp and rename on success.
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_text(output, encoding="utf-8")
    os.replace(tmp_path, out_path)

    rel = str(out_path.relative_to(data_root()))
    conv_meta["summary_file"] = rel
    conv_meta["summary_chars"] = summary_metrics["chars"]
    conv_meta["summary_words"] = summary_metrics["words"]
    conv_meta["summary_tokens_est"] = summary_metrics["tokens_est"]
    conv_meta["compression_ratio"] = ratio
    conv_meta["summarized_at"] = now_iso()

    title = conv_meta.get("title") or "(untitled)"
    print(f"  ✓ {uuid[:8]} — \"{title[:60]}\" → {rel}", flush=True)
    return True


def run(args: Any) -> None:
    ensure_dirs()
    state = state_mod.load()

    # Reconcile state with disk before deciding what's stale: if a summary's
    # .md file was deleted (or moved), drop the freshness marker so the work
    # gets redone. Without this, `summarized_at` could lie about reality.
    reset = state_mod.reconcile_summaries(state)
    if reset:
        state_mod.save(state)
        print(
            f"Reconciled state: {len(reset)} summary file(s) missing on disk, "
            f"marked stale.",
            flush=True,
        )

    try:
        targets, in_scope, scope_desc = _select_targets(state, args)
    except SystemExit:
        raise

    if not targets:
        if in_scope == 0:
            print(
                f"No conversations found in {scope_desc}. "
                f"Nothing was ingested for that scope — check `chronicle stale` "
                f"or widen the range."
            )
        else:
            print(
                f"All {in_scope} conversation(s) in {scope_desc} are already "
                f"summarized and fresh (summarized_at ≥ updated_at). "
                f"To force a re-summary: delete the .md file under "
                f"data/summaries/ (state will reconcile on next run), or "
                f"delete `summarized_at` for the UUID in data/state.json."
            )
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
    workers = max(1, int(getattr(args, "workers", 1) or 1))
    model = getattr(args, "model", None) or "sonnet"
    print(
        f"Summarizing {len(targets)} conversation(s). "
        f"Model: {model} · workers: {workers}"
    )
    succeeded = 0
    failed = 0
    state_lock = threading.Lock()

    def _task(uuid: str) -> bool:
        # summarize_one mutates state["conversations"][uuid] — that's a
        # different key per task, so concurrent mutation is safe. The lock
        # only guards the save.
        ok = summarize_one(
            uuid, state, pending_context=pending_context,
            model=model,
        )
        if ok:
            with state_lock:
                state_mod.save(state)
        return ok

    if workers == 1:
        for uuid in targets:
            if _task(uuid):
                succeeded += 1
            else:
                failed += 1
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_task, u): u for u in targets}
            for fut in as_completed(futures):
                if fut.result():
                    succeeded += 1
                else:
                    failed += 1

    # One final rewrite of pending.md so processed UUIDs fall off.
    pending_mod.write_pending(state)
    print(f"\nDone. {succeeded} ok, {failed} failed.")
    if failed:
        print("Re-run `chronicle summarize --uuid <uuid>` to retry specific failures.")
