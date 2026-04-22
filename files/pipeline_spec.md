# Chronicle Local Pipeline — Spec

## Purpose

A local script that processes Chronicle Export JSON files produced by 
the browser extension. It splits the single-file export into 
per-conversation summary inputs, diffs against previous runs, and 
invokes Claude Code to summarize new/updated conversations. At period 
boundaries it invokes Claude Code to synthesize summaries into a period 
entry.

This script is the glue between the browser extension and Claude Code. 
It does no summarization or synthesis itself — it only shuffles files 
and triggers the right Claude Code command with the right arguments.

## Scope

### In scope

- Watch a directory for new Chronicle Export JSON files.
- Parse the export, split into per-conversation files.
- Diff against existing summary files using `updated_at` timestamps.
- Call Claude Code with `summarize.txt` on new or updated conversations.
- On explicit command, call Claude Code with `synthesize.txt` on a 
  period's summaries to produce the final entry.
- Manage the Chronicle directory structure.

### Out of scope

- Generating summaries (Claude Code does that).
- Synthesizing entries (Claude Code does that).
- Fetching conversations from Claude.ai (extension does that).
- Any kind of user interface beyond CLI commands.

## Directory structure

Canonical Chronicle location: `~/Documents/Chronicle/`

```
~/Documents/Chronicle/
├── exports/                          # Raw JSON dumps from the extension
│   ├── chronicle-export-2026-04-21-2026-04-01-to-2026-04-30.json
│   └── chronicle-export-2026-05-15-2026-05-01-to-2026-05-15.json
├── conversations/                    # Per-conversation JSON files (split)
│   ├── 2026-04/
│   │   ├── abc-123.json
│   │   └── def-456.json
│   └── 2026-05/
├── summaries/                        # Per-conversation summary markdown
│   ├── 2026-04/
│   │   ├── abc-123.md
│   │   └── def-456.md
│   └── 2026-05/
├── entries/                          # Final period entries
│   ├── 2026-Q1_Entry.md
│   ├── 2026-April_Entry.md
│   └── 2026-April-first-half_Entry.md
├── state/                            # Pipeline state
│   └── processed.json                # Hash/timestamp of each conversation
├── instructions/
│   ├── summarize.txt
│   └── synthesize.txt
└── README.md
```

Conversations and summaries are folder-partitioned by `YYYY-MM` derived 
from `created_at`. A conversation started in April always lives in 
`2026-04/` even if its last message is in May.

## Commands

The script exposes a CLI with the following subcommands:

### `chronicle ingest [export_file.json]`

Ingest a single export file.

1. Parse the JSON.
2. For each conversation, compute the target folder from `created_at`.
3. Write the conversation's JSON object to 
   `conversations/YYYY-MM/{uuid}.json`, overwriting if it exists.
4. Check `state/processed.json` for this UUID's last known `updated_at`.
5. If the UUID is new OR the new `updated_at` is later than stored, mark 
   it as needing summarization.
6. After processing all conversations, print a summary:
   `Ingested: 47 conversations. New: 12. Updated: 3. Unchanged: 32.`
7. Do not call Claude Code automatically. Return the list of UUIDs 
   needing summarization to the caller.

### `chronicle watch`

Watch `exports/` for new files. When a new file matching the pattern 
`chronicle-export-*.json` appears, run `ingest` on it automatically, 
then run `summarize --pending`.

### `chronicle summarize --pending`

Call Claude Code with `instructions/summarize.txt` on each conversation 
file that was marked as needing summarization by the last ingest. On 
success, update `state/processed.json` with the new `updated_at`. On 
failure, leave the state unchanged so the next run retries.

### `chronicle summarize --uuid [uuid]`

Force-summarize a specific conversation regardless of state. Useful for 
debugging or regenerating a summary after editing the summarize 
instructions.

### `chronicle synthesize --period [period-label] --range [start] [end]`

Call Claude Code with `instructions/synthesize.txt` on all summary files 
whose `created_at` falls within the given range. Output to 
`entries/[period-label]_Entry.md`.

Examples:

```
chronicle synthesize --period 2026-April --range 2026-04-01 2026-04-30
chronicle synthesize --period 2026-Q2 --range 2026-04-01 2026-06-30
chronicle synthesize --period 2026-April-first-half --range 2026-04-01 2026-04-15
```

### `chronicle status`

Print current pipeline state:
- Number of conversations tracked
- Number pending summarization
- Last ingest timestamp
- Entries written

## State file

`state/processed.json` tracks which conversations have been summarized 
and at what `updated_at`. Format:

```json
{
  "last_ingest": "2026-04-21T14:32:00Z",
  "conversations": {
    "abc-123": {
      "updated_at": "2026-04-08T16:45:00Z",
      "summarized_at": "2026-04-21T14:33:00Z",
      "summary_file": "summaries/2026-04/abc-123.md"
    }
  }
}
```

## Claude Code invocation

The script shells out to the `claude` CLI. The expected command shape:

```
claude --no-interactive \
  --instruction-file instructions/summarize.txt \
  --input conversations/2026-04/abc-123.json \
  --output summaries/2026-04/abc-123.md
```

Exact flags depend on Claude Code's current CLI. The script should 
abstract this behind a function so it's easy to update if Claude Code's 
interface changes.

For synthesize:

```
claude --no-interactive \
  --instruction-file instructions/synthesize.txt \
  --input-dir summaries/ \
  --filter-by-created-at-range 2026-04-01 2026-04-30 \
  --output entries/2026-April_Entry.md
```

If Claude Code doesn't support a range filter natively, the script 
pre-filters the summaries into a temp directory and passes that.

## Implementation notes

- **Language:** Python. Rich stdlib for JSON parsing, file watching 
  (via `watchdog`), and subprocess handling.
- **Dependencies:** Keep minimal. `watchdog` for the watch command is 
  the main one. Everything else can be stdlib.
- **Error handling:** Fail loudly. Print full errors to stderr. Don't 
  swallow exceptions silently — this is a personal tool, not a 
  production service.
- **Idempotency:** Running `ingest` on the same export file twice should 
  be a no-op after the first run. Running `summarize --pending` when 
  nothing is pending should be a no-op.
- **Logging:** Log every action to `state/pipeline.log` with timestamps. 
  Useful when debugging why something didn't get summarized.

## Dev flow

1. Build and test `ingest` first against a sample export file.
2. Add `status` for visibility.
3. Wire up `summarize --pending` and verify Claude Code is producing 
   summary files in the expected format.
4. Add `synthesize` and test on a small range.
5. Add `watch` last — it's just a wrapper over `ingest` + 
   `summarize --pending`.

## Failure modes to handle

- Export file is malformed → skip, log, continue watching.
- Claude Code call fails → leave state unchanged, retry on next run.
- Conversation UUID collides across exports with different `created_at` 
  → trust the most recent export's `created_at`, move the file if 
  needed.
- User deletes a summary file → next ingest won't notice; add a 
  `--rebuild` flag to `summarize` that ignores state and regenerates 
  everything.
