> **HISTORICAL DESIGN SEED — not current docs.** This is the original
> pre-implementation spec for the browser extension. The shipped
> extension may differ. Kept for provenance; treat the code in
> `extension/` as the source of truth.

# Chronicle Export Extension — Spec

## Purpose

A Firefox browser extension that exports the user's complete Claude.ai 
conversation history (or a date-bounded subset) as a single JSON file 
downloaded to the local Downloads folder. The extension is the *capture* 
layer of the Chromeris personal encyclopedia pipeline. Downstream 
processing (splitting, diffing, summarization, synthesis) is handled by 
a separate local script and Claude Code. The extension does not own any 
of that logic.

## Scope

### In scope

- Authenticate using the user's existing Claude.ai browser session (no 
  separate login flow).
- Enumerate all conversations across all contexts accessible to the 
  user: general chat and every project the user has access to.
- Fetch full message content for each conversation, including artifacts 
  (as their source code).
- Bundle results into a single JSON file and trigger a browser download.
- Allow the user to specify a date range (by `updated_at` or 
  `created_at`) to limit the export.

### Out of scope

- Splitting the export into per-conversation files.
- Diffing against previous exports.
- Any summarization or synthesis.
- Native messaging, filesystem writes outside the Downloads folder.
- Scheduled or automatic runs (the user clicks a button).

## Starting point

Fork `agoramachina/claude-exporter` on GitHub. It already handles:

- Session-based authentication via the user's active Claude.ai tab.
- Bulk export of conversations as a ZIP.
- Artifact extraction.
- A browsable conversation table.

The gap to close is **cross-project pagination by date range**. The 
existing extension exports conversations the user opens or selects 
manually. Chronicle Export needs to walk every project + general chat 
automatically within a date window.

## Architecture

### Browser

Firefox primary target. Chrome compatibility is a nice-to-have but not 
required. Use WebExtensions API (Manifest V3) for forward compatibility.

### Components

- **Popup UI** — minimal form with start date, end date, and an Export 
  button. Shows progress during the run.
- **Background script** — orchestrates the full export: enumerate 
  projects, paginate conversations, fetch message content, bundle JSON.
- **Content script** — only needed if session tokens must be read from 
  the active Claude.ai tab. Prefer using cookies via the extension's 
  cookie permission if possible.

### API endpoints to hit

Based on reverse engineering from the existing extensions in this space, 
the relevant Claude.ai internal endpoints are:

- `GET /api/organizations` — returns user's organization(s). Get the 
  `uuid` for subsequent calls.
- `GET /api/organizations/{org_id}/projects` — list all projects the 
  user has access to.
- `GET /api/organizations/{org_id}/chat_conversations` — list 
  conversations in the general context. Supports pagination.
- `GET /api/organizations/{org_id}/projects/{project_id}/chat_conversations` 
  — list conversations within a specific project.
- `GET /api/organizations/{org_id}/chat_conversations/{chat_id}?tree=True` 
  — full conversation content including message tree and artifacts.

These endpoints are not officially documented. Confirm the exact shape 
by inspecting DevTools Network tab on claude.ai before building.

### Rate limiting

Pace the fetch loop. Target one request per 500ms minimum between 
conversation detail fetches. The enumeration step (listing conversations) 
is cheap; the per-conversation content fetch is where volume adds up. 
For a quarter of ~50 conversations, total runtime will be roughly 30-60 
seconds. That's fine.

If any request returns a 429 or unusual status, back off exponentially 
(1s, 2s, 4s, 8s) and retry up to 3 times before failing the whole export.

## Export file format

This is the contract between the extension and the local pipeline. The 
local pipeline depends on this shape. Keep it stable.

### Filename

`chronicle-export-[YYYY-MM-DD]-[start_date]-to-[end_date].json`

Example: `chronicle-export-2026-04-21-2026-04-01-to-2026-04-30.json`

### Structure

```json
{
  "export_metadata": {
    "exported_at": "2026-04-21T14:32:00Z",
    "range_start": "2026-04-01T00:00:00Z",
    "range_end": "2026-04-30T23:59:59Z",
    "range_field": "updated_at",
    "total_conversations": 47,
    "extension_version": "1.0.0"
  },
  "conversations": [
    {
      "uuid": "abc-123-...",
      "title": "...",
      "created_at": "2026-04-05T09:12:00Z",
      "updated_at": "2026-04-08T16:45:00Z",
      "project_uuid": "proj-xyz-..." | null,
      "project_name": "Chromeris" | null,
      "model": "claude-opus-4-7",
      "messages": [
        {
          "uuid": "msg-...",
          "sender": "human" | "assistant",
          "created_at": "...",
          "content": [
            {
              "type": "text",
              "text": "..."
            },
            {
              "type": "artifact",
              "artifact_id": "...",
              "title": "...",
              "language": "jsx",
              "content": "..."
            },
            {
              "type": "tool_use",
              "tool_name": "...",
              "input": {...}
            }
          ]
        }
      ]
    }
  ]
}
```

### Field notes

- `project_uuid` and `project_name` are `null` for general chat 
  conversations.
- `messages[].content` is an array to handle multi-part messages (text 
  plus artifact, text plus tool use, etc).
- Artifacts are stored as source code in a `content` string, not rendered 
  output. No screenshots. If the artifact has multiple versions in a 
  conversation, capture the latest version only.
- The date range filter should apply to `updated_at` by default. Expose 
  `range_field` in the metadata so the local pipeline knows which field 
  was used.

## UI flow

1. User clicks the extension icon.
2. Popup shows two date pickers (start and end), a dropdown for 
   `created_at` vs `updated_at`, and an Export button.
3. On click, the button becomes a progress indicator: 
   "Found N conversations... Fetching 12/47..."
4. On success, the JSON downloads to the Downloads folder. Popup shows 
   the filename and total count.
5. On failure, popup shows the error and offers a Retry button.

## Development notes

- Don't ship to an extension store. Side-load for personal use only.
- Build with standard web tooling (npm, esbuild or similar). Minimal 
  dependencies.
- Log verbosely to the extension's background console during development. 
  Silent on success in production.
- Test with a small date range first (one week) before attempting a full 
  quarter to validate pagination and rate limiting.

## Non-goals

- No UI for browsing conversations in the extension itself. That's what 
  claude.ai is for.
- No export format options (markdown, PDF, etc). JSON only.
- No encryption or special privacy handling. The file sits in Downloads 
  like any other file.
- No auto-trigger or watch mode. Pure manual click-to-export.
