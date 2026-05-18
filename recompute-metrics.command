#!/bin/zsh
# Obsidian "recompute metrics" button target.
# Recomputes word/char/ratio metrics from current file content after a
# hand-edit in the vault. Pure arithmetic, no Claude call, idempotent.
cd "$(dirname "$0")"
/Users/alejandro/.local/bin/uv run --project pipeline chronicle recompute-metrics
echo
read "?[done — press enter to close]"
