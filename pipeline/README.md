# Chronicle pipeline

Python CLI that processes exports from the Chronicle Firefox extension into
per-conversation files, tracks freshness from timestamps, and orchestrates
Claude Code for summarize and synthesize passes.

## Install

```bash
# From the repo root:
uv venv                       # creates .venv/ (skip if you already have one)
uv pip install -e ./pipeline
# Verify:
uv run chronicle --help
```

Python 3.11+ required. No third-party dependencies. Uses [uv](https://github.com/astral-sh/uv);
once installed, invoke the CLI as `uv run chronicle ...` (or activate the venv and run `chronicle`).

### Claude Code login (required for summarize/synthesize)

The summarize and synthesize passes shell out to the `claude` CLI, which needs its own
persistent login. Install [Claude Code](https://claude.com/claude-code), then log in from
a normal terminal:

```bash
claude          # then type /login and complete the browser flow
```

Verify it can authenticate standalone:

```bash
claude -p "reply with exactly: OK"     # should print OK, not a 401
```

If summarize fails with `401 Invalid authentication credentials`, this login is missing or
expired — re-run `claude` / `/login`. (Ingest, search, and the MCP tools don't call Claude
and work without it.)

## Directory layout (under `data/`)

```
data/
├── exports/                  # Raw JSON from the extension. Drop files here.
├── conversations/YYYY-MM/    # Per-conversation JSON, split on ingest.
│   └── deleted/              # Tombstoned conversations (soft-delete).
├── summaries/YYYY-MM/        # Per-conversation markdown, written by Claude.
│   └── deleted/              # Tombstoned summaries.
├── entries/                  # Period entries, written by Claude.
├── state.json                # Freshness ledger.
└── pending.md                # Current delta — what needs summarizing/synthesizing.
```

## Commands

### `chronicle` / `chronicle sync`
The full loop, and the no-arg default. Ingests the latest export, summarizes every
stale conversation, and rebuilds the search index — the one command to run after each
export. It calls Claude, so it spends money and can take minutes on a large batch.

- `--ingest-only` (`-i`) — stop after ingest; no Claude, no spend (the old default).
- `--dry-run` (`-n`) — ingest, then list what would be summarized without calling Claude.
- `--workers N` / `--model M` — passed through to the summarize pass.

Period rollups are deliberately *not* part of this loop — run `chronicle synthesize`
when you want to refresh the half-month/quarter/year entries.

### `chronicle ingest [path]`
Parse one export (or every unprocessed file in `data/exports/`), split into
per-conversation files, update `state.json`, regenerate `pending.md`, fire a
macOS notification. Does not call Claude.

### `chronicle status`
Print pipeline state: conversation counts, stale summaries, period entries,
unprocessed exports, whether the `claude` binary is available, and whether
the launchd auto-ingest agent is installed.

### `chronicle summarize`
Run Claude to generate per-conversation summaries. Only command that calls
Claude (and `synthesize`). Targets are explicit:

- `--uuid UUID` — one conversation
- `--period YYYY-MM` — every stale conversation in that month
- `--all-stale` — default; every conversation with `summary_stale == true`
- `--budget 0.50` — max USD per invocation (default 0.50)

### `chronicle synthesize --period LABEL --range START END`
Build a period entry from the fresh summaries in the range. Refuses to run
if any conversation in the period has a stale summary — run `summarize`
first. Default budget: 2.00 USD.

### `chronicle install-agent` / `chronicle uninstall-agent`
macOS-only. Installs a launchd agent that watches `data/exports/` for new
files and runs `chronicle ingest` automatically. Logs to
`~/Library/Logs/Chronicle/`.

## Freshness model

There are no stale-bits. Staleness is derived from timestamps:

- A summary is stale iff `conversation.updated_at > summary.summarized_at`.
- A period entry is stale iff any conversation in the entry's date range has
  `updated_at > entry.synthesized_at`.

Deletions: the extension reports `deleted_uuids` in export metadata. Ingest
soft-deletes them by moving the JSON (and any summary) into `deleted/`
subdirectories and recording `deleted_at` in state. Nothing is ever
unlinked.

## Security posture

Every `claude -p` invocation runs with
`--disallowedTools Bash,Write,Edit,NotebookEdit` and `--max-budget-usd`.
Conversation content is treated as attacker-controllable (anything you've
ever pasted into Claude); the Python wrapper is the only thing that writes
files. See `chronicle/claude_invoke.py`.
