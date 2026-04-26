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
        from .calendar import PeriodParseError, parse_period
        try:
            _tier, rs, re_ = parse_period(args.period)
        except PeriodParseError as e:
            raise SystemExit(str(e))
        rows = state_mod.conversations_in_period(state, rs, re_)
        return [uuid for uuid, c in rows if state_mod.summary_stale(c)]
    # default: all stale
    return state_mod.stale_summary_uuids(state)


def summarize_one(
    uuid: str,
    state: dict[str, Any],
    *,
    pending_context: str,
    budget_usd: float,
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
            max_budget_usd=budget_usd,
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
    workers = max(1, int(getattr(args, "workers", 1) or 1))
    model = getattr(args, "model", None) or "sonnet"
    print(
        f"Summarizing {len(targets)} conversation(s). "
        f"Model: {model} · Budget per call: ${args.budget:.2f} · workers: {workers}"
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
            budget_usd=args.budget, model=model,
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
