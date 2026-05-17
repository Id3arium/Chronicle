"""Central filesystem layout for Chronicle.

The repo root is resolved by walking up from this file until we find the
pipeline/ directory's parent. An env var CHRONICLE_ROOT overrides for tests.
"""

import os
import re
from pathlib import Path


def slugify(title: str | None, *, max_len: int = 60) -> str:
    """Title → filesystem-safe slug. Empty/None → 'untitled'."""
    if not title:
        return "untitled"
    s = title.lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    s = re.sub(r"-+", "-", s)
    if not s:
        return "untitled"
    return s[:max_len].rstrip("-") or "untitled"


def stem_for(uuid: str, title: str | None, created_at: str | None = None) -> str:
    """Stable filename stem: {DD}_{slug}__{uuid8}.

    DD is the zero-padded day-of-month from `created_at` (ISO 8601). It makes
    `ls` inside a YYYY-MM directory show conversations in chronological order
    without needing to read frontmatter or sort by mtime. Falls back to "00"
    if created_at is missing or unparseable — that sorts to the top, making
    bad data visually obvious.

    UUID suffix prevents collisions and lets us find the file by UUID even
    if the title changed upstream.
    """
    day = "00"
    if created_at and len(created_at) >= 10:
        # ISO 8601: YYYY-MM-DD... → take chars 8-9.
        try:
            d = int(created_at[8:10])
            if 1 <= d <= 31:
                day = f"{d:02d}"
        except ValueError:
            pass
    return f"{day}_{slugify(title)}__{uuid[:8]}"


def repo_root() -> Path:
    override = os.environ.get("CHRONICLE_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    # pipeline/chronicle/paths.py → repo root is two parents up.
    return Path(__file__).resolve().parent.parent.parent


def vault_root() -> Path:
    """The Obsidian vault: the human-readable tree (summaries, entries) plus
    the raw conversations the summary→`## Full conversation` wikilinks point
    at. Obsidian only resolves links to files INSIDE the vault, so the
    conversation leaves must live here even though they aren't read directly.
    Open this directory as the vault in Obsidian."""
    return repo_root() / "library"


def work_root() -> Path:
    """Machine-only pipeline plumbing: the inbox queue, incremental-summary
    scratch (branches/segments), and the state/index/pending files. Never
    opened in Obsidian — kept out of the vault so it doesn't clutter what
    you read."""
    return repo_root() / "pipeline-data"


def data_root() -> Path:
    """Back-compat alias. State stores conversation/summary/entry paths
    relative to the vault root (`summaries/…`, `conversations/…`,
    `entries/…`), so resolving them against the vault keeps every stored
    path valid after the library/ + pipeline-data/ split."""
    return vault_root()


def exports_dir() -> Path:
    """Legacy alias — use inbox_dir()."""
    return inbox_dir()


def inbox_dir() -> Path:
    return work_root() / "inbox"


def conversations_dir() -> Path:
    return vault_root() / "conversations"


def deleted_conversations_dir() -> Path:
    return conversations_dir() / "deleted"


def summaries_dir() -> Path:
    return vault_root() / "summaries"


def deleted_summaries_dir() -> Path:
    return summaries_dir() / "deleted"


def diffs_dir() -> Path:
    """Legacy — kept for migration cleanup only."""
    return work_root() / "diffs"


def branches_dir() -> Path:
    return work_root() / "branches"


def segments_dir() -> Path:
    return work_root() / "segments"


def entries_dir() -> Path:
    return vault_root() / "entries"


def state_file() -> Path:
    return work_root() / "state.json"


def index_file() -> Path:
    return work_root() / "index.json"


def pending_file() -> Path:
    return work_root() / "pending.md"


def glossary_file() -> Path:
    """Project/term glossary loaded ONLY on synthesize passes. Summaries stay
    self-contained — they get a 'never invent meanings, carry verbatim' rule
    instead, to keep summarize-tier token cost flat."""
    return vault_root() / "glossary.md"


def instructions_dir() -> Path:
    return repo_root() / "files"


def ensure_dirs() -> None:
    for d in (
        inbox_dir(),
        conversations_dir(),
        deleted_conversations_dir(),
        summaries_dir(),
        deleted_summaries_dir(),
        diffs_dir(),
        branches_dir(),
        entries_dir(),
    ):
        d.mkdir(parents=True, exist_ok=True)
