"""`chronicle backfill-keywords` — extract keywords from existing summaries.

Reads each summary's body text, calls Claude to extract keywords, and
writes them back into the frontmatter (replacing `tags` if present).
This is a one-time migration step; future summaries will have keywords
generated during summarization.

Only processes summaries that have no `keywords` field yet.
Use --force to re-extract even if keywords already exist.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from . import state as state_mod
from .claude_invoke import ClaudeInvocationError, run_claude
from .metrics import parse_frontmatter
from .paths import data_root, instructions_dir


_EXTRACT_PROMPT = """\
You are extracting search keywords from a Chronicle summary.

Read the summary below and produce a single line of comma-separated keywords.
These keywords power Chronicle's search index. Include:

- People mentioned by name (e.g. Michael Levin, Mandelbrot)
- Frameworks, theories, models (e.g. FOP, bioelectricity, dissipative structures)
- Tools, projects, languages, platforms (e.g. Chronicle, Python, xcodegen, Asana)
- Key concepts and their associated domains — if a person is mentioned, also
  include the concepts they're known for (e.g. if Levin → also bioelectricity,
  morphogenesis, collective intelligence)
- Coinages, named frames, specific terms that crystallized in the conversation
- Concrete artifacts (files, scripts, documents named)

Do NOT include:
- Generic words (conversation, discussion, analysis, approach)
- Categories that are already in the frontmatter (software, personal, etc.)
- Stop words or filler

Output ONLY the comma-separated keywords line. Nothing else. No preamble,
no explanation, no formatting. Example output:
Michael Levin, bioelectricity, morphogenesis, FOP, fractal organization, collective intelligence, Claude skills
"""


def _extract_keywords_from_summary(
    summary_text: str,
    model: str | None = None,
) -> str | None:
    """Call Claude to extract keywords from a summary. Returns the keywords
    string, or None on failure. Rate-limit retries handled by run_claude."""
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(_EXTRACT_PROMPT)
        prompt_path = Path(f.name)

    try:
        output = run_claude(
            prompt_path,
            summary_text,
            model=model,
            max_budget_usd=0.05,
            timeout_seconds=120,
        )
        return output.strip()
    except ClaudeInvocationError:
        return None
    finally:
        prompt_path.unlink(missing_ok=True)


def _replace_tags_with_keywords(summary_text: str, keywords: str) -> str:
    """Replace `tags:` line with `keywords:` in frontmatter, or add keywords
    if neither exists."""
    if not summary_text.lstrip().startswith("---\n"):
        return summary_text

    rest = summary_text.lstrip()[4:]
    end = rest.find("\n---")
    if end == -1:
        return summary_text

    fm_block = rest[:end]
    after_fm = rest[end:]

    # Remove existing tags line.
    fm_block = re.sub(r"^tags:.*\n?", "", fm_block, flags=re.MULTILINE)
    # Remove existing keywords line (if --force re-run).
    fm_block = re.sub(r"^keywords:.*\n?", "", fm_block, flags=re.MULTILINE)

    # Insert keywords before significance line, or at the end of frontmatter.
    kw_line = f"keywords: {keywords}\n"
    sig_match = re.search(r"^significance:", fm_block, re.MULTILINE)
    if sig_match:
        pos = sig_match.start()
        fm_block = fm_block[:pos] + kw_line + fm_block[pos:]
    else:
        fm_block = fm_block.rstrip("\n") + "\n" + kw_line

    return "---\n" + fm_block + after_fm


def run(args: Any) -> None:
    state = state_mod.load()
    force = getattr(args, "force", False)
    model = getattr(args, "model", None) or "haiku"
    workers = max(1, int(getattr(args, "workers", 1) or 1))

    targets: list[tuple[str, str]] = []  # (uuid, summary_path)
    for uuid, c in state["conversations"].items():
        if c.get("deleted_at"):
            continue
        sf = c.get("summary_file")
        if not sf:
            continue
        path = data_root() / sf
        if not path.exists():
            continue
        if not force:
            fm = parse_frontmatter(path.read_text(encoding="utf-8"))
            if fm.get("keywords"):
                continue
        targets.append((uuid, str(path)))

    if not targets:
        print("All summaries already have keywords. Use --force to re-extract.")
        return

    print(f"Backfilling keywords for {len(targets)} summaries. Model: {model} · workers: {workers}")
    print(f"Resumable: re-run the same command to pick up where you left off.\n")

    succeeded = 0
    failed = 0
    consecutive_failures = 0
    max_consecutive_failures = 5

    def _process(uuid: str, path_str: str) -> bool:
        path = Path(path_str)
        summary_text = path.read_text(encoding="utf-8")
        title = parse_frontmatter(summary_text).get("title", uuid[:8])

        keywords = _extract_keywords_from_summary(summary_text, model=model)
        if not keywords:
            print(f"  ✗ {uuid[:8]} — \"{title[:50]}\" — claude error", flush=True)
            return False

        updated = _replace_tags_with_keywords(summary_text, keywords)
        # Atomic write.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(updated, encoding="utf-8")
        os.replace(tmp, path)

        n_kw = len([k for k in keywords.split(",") if k.strip()])
        print(f"  ✓ {uuid[:8]} — \"{title[:50]}\" — {n_kw} keywords", flush=True)
        return True

    # Always run sequentially — rate limits make parallelism counterproductive
    # for API-bound work like this. Keeps the consecutive-failure logic simple.
    for i, (uuid, path_str) in enumerate(targets):
        if _process(uuid, path_str):
            succeeded += 1
            consecutive_failures = 0
        else:
            failed += 1
            consecutive_failures += 1
            if consecutive_failures >= max_consecutive_failures:
                remaining = len(targets) - i - 1
                print(
                    f"\n⚠ {max_consecutive_failures} consecutive failures — "
                    f"stopping early. {remaining} summaries remaining.",
                    flush=True,
                )
                print(f"Re-run `chronicle backfill-keywords` to resume.", flush=True)
                break

    print(f"\nDone. {succeeded} ok, {failed} failed.")

    if succeeded > 0:
        print("Rebuilding index...")
        from .index import build_index
        build_index(state)
        print("Index rebuilt.")
