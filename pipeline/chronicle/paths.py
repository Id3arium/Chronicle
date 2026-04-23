"""Central filesystem layout for Chronicle.

The repo root is resolved by walking up from this file until we find the
pipeline/ directory's parent. An env var CHRONICLE_ROOT overrides for tests.
"""

import os
from pathlib import Path


def repo_root() -> Path:
    override = os.environ.get("CHRONICLE_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    # pipeline/chronicle/paths.py → repo root is two parents up.
    return Path(__file__).resolve().parent.parent.parent


def data_root() -> Path:
    return repo_root() / "data"


def exports_dir() -> Path:
    return data_root() / "exports"


def conversations_dir() -> Path:
    return data_root() / "conversations"


def deleted_conversations_dir() -> Path:
    return conversations_dir() / "deleted"


def summaries_dir() -> Path:
    return data_root() / "summaries"


def deleted_summaries_dir() -> Path:
    return summaries_dir() / "deleted"


def entries_dir() -> Path:
    return data_root() / "entries"


def state_file() -> Path:
    return data_root() / "state.json"


def pending_file() -> Path:
    return data_root() / "pending.md"


def instructions_dir() -> Path:
    return repo_root() / "files"


def ensure_dirs() -> None:
    for d in (
        exports_dir(),
        conversations_dir(),
        deleted_conversations_dir(),
        summaries_dir(),
        deleted_summaries_dir(),
        entries_dir(),
    ):
        d.mkdir(parents=True, exist_ok=True)
