"""`chronicle recompute-metrics` — recompute length metrics from current
file content, no Claude call.

Metrics are written once at generation time (summarize/synthesize) into
both the file frontmatter and the state record. If you hand-edit a summary
or entry afterward — fix a typo, cut a paragraph — the recorded
word/char/ratio numbers no longer match the file, and the lie propagates
upward: a half entry's `sources_summary_words` is summed from child
summaries' state, a quarter's totals are summed from its halves' frontmatter.

This recomputes everything bottom-up so the cascade settles in one pass:

  1. Summaries: re-measure the file, recompute `summary_chars/words/
     tokens_est` and `compression_ratio`. The ratio's denominator
     (`original_chars`) comes from state — the source conversation isn't
     edited, so it stays valid. Rewrite frontmatter + state.
  2. Entries, lowest tier first (half → quarter → year): recompute
     `entry_words`/`entry_compression_ratio` from the current body, and
     re-roll the source totals from the now-corrected children/summaries.
     Rewrite frontmatter + state.

Pure arithmetic over numbers already on disk / in state — microseconds,
not a re-synthesis. Idempotent: a file already consistent is left byte-
for-byte unchanged. Run: `uv run chronicle recompute-metrics`.
"""

from __future__ import annotations

import os
from typing import Any

from . import state as state_mod
from .calendar import parse_period
from .metrics import (
    compression_ratio,
    entry_body,
    measure_text,
    parse_frontmatter,
    render_with_frontmatter,
    split_frontmatter,
)
from .paths import data_root

# Synthesize tiers, ordered so a parent is recomputed only after its
# children are already correct.
_TIER_ORDER = {"half": 0, "quarter": 1, "year": 2}


def _rewrite_if_changed(rel_path: str, new_fields: dict[str, Any], body: str) -> bool:
    """Reserialize frontmatter+body and write only if the bytes differ.
    Returns True if the file was rewritten."""
    path = data_root() / rel_path
    new_text = render_with_frontmatter(new_fields, body)
    if path.exists() and path.read_text(encoding="utf-8") == new_text:
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, path)
    return True


def _recompute_summaries(state: dict[str, Any]) -> tuple[int, int]:
    """Returns (files_rewritten, state_records_changed)."""
    files = records = 0
    for uuid, c in state["conversations"].items():
        if c.get("deleted_at") or not c.get("summary_file"):
            continue
        if not c.get("summarized_at"):
            continue
        path = data_root() / c["summary_file"]
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        fields, body = split_frontmatter(text)
        if not fields:
            print(f"  · skip (no frontmatter): {c['summary_file']}")
            continue

        # Measure the BODY, not the whole file. The frontmatter holds the
        # metrics themselves — measuring it would make summary_chars depend
        # on the ratio string, whose own length depends on summary_chars: a
        # feedback loop that never converges (0.3958 → 0.396 → …). The body
        # is the stable, self-consistent unit and is also the apples-to-
        # apples prose count against the original.
        # Also strip the stats subheader before measuring — it's metadata,
        # not prose, and including it would create the same feedback loop.
        from .summarize import _SUMMARY_SUBHEADER_RE, _inject_summary_subheader
        body_clean = _SUMMARY_SUBHEADER_RE.sub("", body, count=1).lstrip("\n")
        m = measure_text(body_clean)
        orig_chars = c.get("original_chars") or 0
        orig_words = c.get("original_words") or 0
        ratio = compression_ratio(m["chars"], orig_chars)

        new_fields = dict(fields)
        new_fields["summary_words"] = m["words"]
        new_fields["compression_ratio"] = ratio
        # Re-inject the stats subheader with corrected values.
        final_body = _SUMMARY_SUBHEADER_RE.sub("", body, count=1).lstrip("\n")
        stats = f"**{orig_words:,} words → {m['words']:,} words · {ratio:.4f} ratio**"
        final_body = f"{stats}\n\n{final_body}"
        if _rewrite_if_changed(c["summary_file"], new_fields, final_body):
            files += 1

        before = (
            c.get("summary_chars"),
            c.get("summary_words"),
            c.get("summary_tokens_est"),
            c.get("compression_ratio"),
        )
        c["summary_chars"] = m["chars"]
        c["summary_words"] = m["words"]
        c["summary_tokens_est"] = m["tokens_est"]
        c["compression_ratio"] = ratio
        after = (m["chars"], m["words"], m["tokens_est"], ratio)
        if before != after:
            records += 1
    return files, records


def _child_source_metrics(state: dict[str, Any], child_label: str) -> dict[str, int | float]:
    """A rollup entry's per-child source_metrics, mirroring synthesize's
    _gather_rollup_inputs: original_words = child's transitive conversation
    total, summary_words = child entry's own word count, compression_ratio =
    child's own entry ratio. Read from the child's (now-corrected) state."""
    e = state["entries"].get(child_label, {})
    return {
        "original_words": e.get("sources_conversation_words") or e.get("total_source_conversation_words", 0) or 0,
        "summary_words": e.get("entry_words", 0) or 0,
        "compression_ratio": e.get("entry_compression_ratio", 0.0) or 0.0,
    }


def _half_source_totals(
    state: dict[str, Any], range_start: str, range_end: str
) -> tuple[int, int]:
    """Sum the corrected per-conversation totals for a half entry's range,
    matching _gather_half_inputs (fresh, non-deleted summaries only)."""
    total_orig = total_sum = 0
    for _uuid, c in state_mod.conversations_in_period(state, range_start, range_end):
        if state_mod.summary_stale(c) or not c.get("summary_file"):
            continue
        total_orig += c.get("original_words") or 0
        total_sum += c.get("summary_words") or 0
    return total_orig, total_sum


def _recompute_entries(state: dict[str, Any]) -> tuple[int, int]:
    """Returns (files_rewritten, state_records_changed). Lowest tier first
    so a parent rolls up already-corrected children."""
    entries = state.get("entries", {})

    def _tier_key(item: tuple[str, dict]) -> int:
        return _TIER_ORDER.get(item[1].get("tier", ""), 99)

    files = records = 0
    for label, e in sorted(entries.items(), key=_tier_key):
        entry_file = e.get("entry_file")
        if not entry_file:
            continue
        path = data_root() / entry_file
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        fields, body = split_frontmatter(text)
        if not fields:
            print(f"  · skip (no frontmatter): {entry_file}")
            continue

        tier = e.get("tier") or fields.get("tier", "")
        try:
            _t, rs, re_ = parse_period(label)
        except Exception:
            rs = e.get("range_start", "")
            re_ = e.get("range_end", "")

        if tier == "half":
            total_orig, total_sum = _half_source_totals(state, rs, re_)
        else:
            children = e.get("children") or []
            # Resolve merged H1-H2 entries the same way synthesize does —
            # if children says [2025_01_H1, 2025_01_H2] but only
            # 2025_01_H1-H2 exists, use the merged entry.
            from .synthesize import _resolve_children_with_merges
            resolved = _resolve_children_with_merges(state, children)
            sm = [_child_source_metrics(state, ch) for ch in resolved]
            total_orig = sum(int(s["original_words"]) for s in sm)
            total_sum = sum(int(s["summary_words"]) for s in sm)

        entry_words = measure_text(entry_body(text))["words"]
        aggregate_ratio = round(total_sum / total_orig, 4) if total_orig else 0.0
        entry_ratio = round(entry_words / total_sum, 4) if total_sum else 0.0
        metrics = {
            "sources_conversation_words": total_orig,
            "sources_summary_words": total_sum,
            "sources_compression_ratio": aggregate_ratio,
            "entry_words": entry_words,
            "entry_compression_ratio": entry_ratio,
        }

        new_fields = dict(fields)
        new_fields.update(metrics)
        # Keep the subheader's word/ratio stats in sync with the metrics.
        from .synthesize import _enrich_subheader
        enriched_body = _enrich_subheader(body, metrics)
        if _rewrite_if_changed(entry_file, new_fields, enriched_body):
            files += 1

        before = {k: e.get(k) for k in metrics}
        if before != metrics:
            e.update(metrics)
            e["entry_chars"] = len(render_with_frontmatter(new_fields, body))
            records += 1
    return files, records


def run_recompute(state: dict[str, Any]) -> dict[str, int]:
    """Recompute all metrics in place. Mutates `state` (caller saves).
    Returns a counts dict. Safe to call from synthesize before a rollup."""
    sf, sr = _recompute_summaries(state)
    ef, er = _recompute_entries(state)
    return {
        "summary_files": sf,
        "summary_records": sr,
        "entry_files": ef,
        "entry_records": er,
    }


def run(args: Any) -> None:
    state = state_mod.load()
    counts = run_recompute(state)
    state_mod.save(state)
    print(
        f"Summaries: {counts['summary_files']} file(s) rewritten, "
        f"{counts['summary_records']} state record(s) updated"
    )
    print(
        f"Entries:   {counts['entry_files']} file(s) rewritten, "
        f"{counts['entry_records']} state record(s) updated"
    )
    total = sum(counts.values())
    if total == 0:
        print("Everything already consistent — nothing changed.")
    else:
        print("Done. Re-running now is a no-op (idempotent).")
