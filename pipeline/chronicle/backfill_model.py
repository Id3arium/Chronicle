"""One-shot: backfill the `model` provenance field onto artifacts that
were produced before the field existed.

Ground truth (supplied by the user, not inferred from content):
- Existing per-conversation summaries: claude-sonnet-4-6. Certain — the
  only Sonnet available when they were generated was 4.6.
- Existing period entries: claude-opus-4-6. User's recollection of what
  synthesize ran on before IDs were pinned.

Idempotent: skips any file/record that already has a `model` value, so
re-running after new summarize/synthesize work won't clobber the exact
IDs those record going forward. Run: `uv run python -m chronicle.backfill_model`.
"""

from __future__ import annotations

import os

from . import state as state_mod
from .metrics import render_with_frontmatter, split_frontmatter
from .paths import data_root

SUMMARY_MODEL = "claude-sonnet-4-6"
ENTRY_MODEL = "claude-opus-4-6"


def _set_model_in_file(rel_path: str, model: str) -> bool:
    """Add `model:` to a file's frontmatter if absent. Returns True if
    the file was rewritten. Uses the same parse/reserialize path as the
    pipeline so a `---` in the body is never mistaken for a fence."""
    path = data_root() / rel_path
    if not path.exists():
        print(f"  · skip (file missing): {rel_path}")
        return False
    text = path.read_text(encoding="utf-8")
    fields, body = split_frontmatter(text)
    if not fields:
        print(f"  · skip (no frontmatter): {rel_path}")
        return False
    if fields.get("model"):
        return False  # already set — leave the exact value alone
    # Insert model right after the identity-ish leading keys by rebuilding
    # the dict: model goes first so it reads near the top, then the rest.
    rebuilt = {"model": model, **fields}
    new_text = render_with_frontmatter(rebuilt, body)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, path)
    return True


def run() -> None:
    state = state_mod.load()

    sum_files = sum_state = 0
    for c in state["conversations"].values():
        if c.get("deleted_at") or not c.get("summary_file"):
            continue
        if not c.get("summarized_at"):
            continue
        if _set_model_in_file(c["summary_file"], SUMMARY_MODEL):
            sum_files += 1
        if not c.get("model"):
            c["model"] = SUMMARY_MODEL
            sum_state += 1

    ent_files = ent_state = 0
    for e in state["entries"].values():
        if not e.get("entry_file"):
            continue
        if _set_model_in_file(e["entry_file"], ENTRY_MODEL):
            ent_files += 1
        if not e.get("model"):
            e["model"] = ENTRY_MODEL
            ent_state += 1

    state_mod.save(state)
    print(
        f"Summaries: {sum_files} file(s) rewritten, {sum_state} state record(s) "
        f"set → {SUMMARY_MODEL}"
    )
    print(
        f"Entries:   {ent_files} file(s) rewritten, {ent_state} state record(s) "
        f"set → {ENTRY_MODEL}"
    )
    print("Done. Re-running is safe — anything already tagged is left as-is.")


if __name__ == "__main__":
    run()
