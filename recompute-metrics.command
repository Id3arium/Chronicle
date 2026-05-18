#!/bin/zsh
# Obsidian "recompute metrics" button target.
# Recomputes word/char/ratio metrics from current file content after a
# hand-edit in the vault. Pure arithmetic, no Claude call, idempotent.
cd "$(dirname "$0")"

# GUI apps (Obsidian) don't inherit the shell PATH, so `uv` may not be
# found. Resolve it: PATH first, then the usual install locations.
uv_bin="$(command -v uv 2>/dev/null)"
if [[ -z "$uv_bin" ]]; then
  for cand in "$HOME/.local/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
    [[ -x "$cand" ]] && uv_bin="$cand" && break
  done
fi
if [[ -z "$uv_bin" ]]; then
  echo "uv not found. Install it (https://github.com/astral-sh/uv) or add it to PATH."
  read "?[press enter to close]"
  exit 1
fi

"$uv_bin" run --project pipeline chronicle recompute-metrics
echo
read "?[done — press enter to close]"
