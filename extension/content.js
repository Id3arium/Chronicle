// Chronicle Export — content script.
// Runs on claude.ai. Performs all authenticated fetches using the user's
// session cookie (credentials: 'include'), writes a structured log to
// chrome.storage.local as it goes, and triggers the download at the end.

// Double-injection guard (pattern from agoramachina/claude-exporter content.js:8-12).
if (window.__chronicleExportLoaded) {
  console.log('[Chronicle] content script already loaded, skipping');
} else {
  window.__chronicleExportLoaded = true;

  const EXTENSION_VERSION = '0.1.0';
  const FETCH_DELAY_MS = 500;
  const BACKOFF_SCHEDULE_MS = [1000, 2000, 4000];
  const MAX_RETRIES = BACKOFF_SCHEDULE_MS.length;
  const LOG_CAP = 500;

  // Filename-safe character regex (from fork content.js:345).
  const INVALID_FILENAME_CHARS = /[<>:"/\\|?*\x00-\x1f]/g;

  function sanitizeFilenameComponent(s) {
    return (s || '').replace(INVALID_FILENAME_CHARS, '_').slice(0, 100);
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  // ──────────────────────────── logging ────────────────────────────

  async function readState() {
    return new Promise((resolve) => {
      chrome.storage.local.get(['currentRun', 'lastRun', 'runLog'], (r) => resolve(r));
    });
  }

  async function writeState(patch) {
    return new Promise((resolve) => chrome.storage.local.set(patch, resolve));
  }

  async function appendLog(entry) {
    const { runLog = [] } = await readState();
    const next = runLog.concat([{ ts: new Date().toISOString(), ...entry }]);
    // FIFO-cap.
    const capped = next.length > LOG_CAP ? next.slice(next.length - LOG_CAP) : next;
    await writeState({ runLog: capped });
    const method = entry.level === 'error' ? 'error' : entry.level === 'warn' ? 'warn' : 'log';
    console[method]('[Chronicle]', entry);
  }

  async function updateCurrentRun(patch) {
    const { currentRun } = await readState();
    if (!currentRun) return;
    await writeState({ currentRun: { ...currentRun, ...patch } });
  }

  // ──────────────────────────── fetch with backoff ────────────────────────────

  async function fetchJsonWithBackoff(url, label) {
    let lastErr = null;
    for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
      try {
        const res = await fetch(url, { credentials: 'include', headers: { Accept: 'application/json' } });
        if (res.ok) return await res.json();

        // Retry on 429 and 5xx; fail fast on other 4xx.
        const retriable = res.status === 429 || (res.status >= 500 && res.status < 600);
        const bodyText = await res.text().catch(() => '');
        const snippet = bodyText.slice(0, 200);
        if (!retriable || attempt === MAX_RETRIES) {
          const err = new Error(`HTTP ${res.status}`);
          err.status = res.status;
          err.bodySnippet = snippet;
          throw err;
        }
        const wait = BACKOFF_SCHEDULE_MS[attempt];
        await appendLog({ level: 'warn', msg: label, status: res.status, retry: `${attempt + 1}/${MAX_RETRIES}`, bodySnippet: snippet });
        await sleep(wait);
      } catch (err) {
        lastErr = err;
        if (err.status !== undefined) throw err; // already handled above
        if (attempt === MAX_RETRIES) break;
        const wait = BACKOFF_SCHEDULE_MS[attempt];
        await appendLog({ level: 'warn', msg: `${label}: ${err.message}`, retry: `${attempt + 1}/${MAX_RETRIES}` });
        await sleep(wait);
      }
    }
    throw lastErr || new Error('fetch failed');
  }

  // ──────────────────────────── API calls ────────────────────────────

  async function getOrgId() {
    // Pattern cherry-picked from fork popup.js:11-35 / content.js:124-150.
    const orgs = await fetchJsonWithBackoff('https://claude.ai/api/organizations', 'list organizations');
    if (!Array.isArray(orgs) || orgs.length === 0) throw new Error('No organizations returned by claude.ai. Make sure you are logged in.');
    const chatOrg = orgs.find((o) => Array.isArray(o.capabilities) && o.capabilities.includes('chat'));
    return (chatOrg || orgs[0]).uuid;
  }

  async function listProjects(orgId) {
    const url = `https://claude.ai/api/organizations/${orgId}/projects`;
    const projects = await fetchJsonWithBackoff(url, 'list projects');
    return Array.isArray(projects) ? projects : [];
  }

  async function listAllConversations(orgId) {
    // Single endpoint returns every conversation across general chat + all projects.
    // Each conversation's project_uuid field tells us which project it belongs to
    // (or null for general chat). Pattern from fork content.js:102.
    const url = `https://claude.ai/api/organizations/${orgId}/chat_conversations`;
    const convs = await fetchJsonWithBackoff(url, 'list conversations');
    return Array.isArray(convs) ? convs : [];
  }

  async function fetchFullConversation(orgId, conversationUuid) {
    // render_all_tools=true ensures tool_use blocks are populated
    // (from fork content.js:84).
    const url = `https://claude.ai/api/organizations/${orgId}/chat_conversations/${conversationUuid}?tree=True&rendering_mode=messages&render_all_tools=true`;
    return await fetchJsonWithBackoff(url, `fetch conversation ${conversationUuid}`);
  }

  // ──────────────────────────── export transform ────────────────────────────

  function normalizeMessages(fullConv) {
    const messages = [];
    const chatMessages = Array.isArray(fullConv.chat_messages) ? fullConv.chat_messages : [];
    for (const msg of chatMessages) {
      const content = [];
      if (Array.isArray(msg.content)) {
        for (const c of msg.content) {
          if (c && c.type === 'text' && typeof c.text === 'string') {
            content.push({ type: 'text', text: c.text });
          } else if (c && c.type === 'thinking' && typeof c.thinking === 'string') {
            content.push({ type: 'thinking', text: c.thinking });
          } else if (c && c.type === 'tool_use') {
            // Carry display_content through; artifact extraction happens downstream.
            const block = { type: 'tool_use', tool_name: c.name || c.tool_name || null };
            if (c.input !== undefined) block.input = c.input;
            if (c.display_content) {
              const dc = c.display_content;
              if (dc.type === 'code_block') {
                content.push({
                  type: 'artifact',
                  artifact_id: c.id || null,
                  title: (dc.filename || '').split('/').pop() || null,
                  language: dc.language || null,
                  content: dc.code || ''
                });
                continue;
              } else if (dc.type === 'json_block' && dc.json_block) {
                try {
                  const parsed = JSON.parse(dc.json_block);
                  if (parsed && parsed.filename) {
                    content.push({
                      type: 'artifact',
                      artifact_id: c.id || null,
                      title: parsed.filename.split('/').pop() || null,
                      language: parsed.language || null,
                      content: parsed.code || ''
                    });
                    continue;
                  }
                } catch (_) { /* fall through to tool_use */ }
              }
            }
            content.push(block);
          }
        }
      } else if (typeof msg.text === 'string') {
        content.push({ type: 'text', text: msg.text });
      }

      messages.push({
        uuid: msg.uuid,
        sender: msg.sender,
        created_at: msg.created_at,
        content
      });
    }
    return messages;
  }

  function buildConversationRecord(fullConv, project) {
    return {
      uuid: fullConv.uuid,
      title: fullConv.name || null,
      created_at: fullConv.created_at,
      updated_at: fullConv.updated_at,
      project_uuid: project ? project.uuid : null,
      project_name: project ? project.name : null,
      model: fullConv.model || null,
      messages: normalizeMessages(fullConv)
    };
  }

  // ──────────────────────────── main run ────────────────────────────

  function dateInRange(iso, startDate, endDate) {
    if (!iso) return false;
    // startDate/endDate are YYYY-MM-DD strings in local time; compare as UTC day boundaries.
    const t = new Date(iso).getTime();
    const start = new Date(`${startDate}T00:00:00Z`).getTime();
    const end = new Date(`${endDate}T23:59:59.999Z`).getTime();
    return t >= start && t <= end;
  }

  function exportFilename(startDate, endDate) {
    return `chronicle-export-${startDate}-to-${endDate}.json`;
  }

  function triggerDownload(jsonString, filename) {
    const safe = sanitizeFilenameComponent(filename) || 'chronicle-export.json';
    const blob = new Blob([jsonString], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = safe;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    // Revoke after a tick so the download has time to start.
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function isCancelled() {
    const { cancelRequested } = await new Promise((resolve) =>
      chrome.storage.local.get(['cancelRequested'], resolve)
    );
    return !!cancelRequested;
  }

  async function clearCancelFlag() {
    await new Promise((resolve) => chrome.storage.local.remove(['cancelRequested'], resolve));
  }

  // Unified export. Incremental-by-default: only fetches conversations whose
  // updated_at moved since last scrape. The `fetchAll` flag skips the date
  // filter; `onlyUuids` (retry case) narrows to a specific UUID whitelist.
  async function runExport({ fetchAll, startDate, endDate, rangeField, onlyUuids }) {
    const startedAt = new Date().toISOString();
    await clearCancelFlag();
    await writeState({
      currentRun: {
        startedAt,
        total: 0,
        completed: 0,
        rangeStart: fetchAll ? null : startDate,
        rangeEnd: fetchAll ? null : endDate,
        rangeField,
        fetchAll: !!fetchAll
      },
      runLog: []
    });
    const rangeLabel = fetchAll ? 'all conversations' : `${rangeField} ${startDate} → ${endDate}`;
    await appendLog({ level: 'ok', msg: `Export started (${rangeLabel})` });

    try {
      const orgId = await getOrgId();
      await appendLog({ level: 'ok', msg: `Using organization ${orgId}` });

      const projects = await listProjects(orgId);
      const projectMap = new Map(projects.map((p) => [p.uuid, p]));
      await appendLog({ level: 'ok', msg: `Found ${projects.length} projects` });

      const allConvs = await listAllConversations(orgId);
      await appendLog({ level: 'ok', msg: `Found ${allConvs.length} conversations across all contexts` });

      const { seenConversations = {} } = await new Promise((resolve) =>
        chrome.storage.local.get(['seenConversations'], resolve)
      );

      // Detect deletions: UUIDs we've seen before that no longer appear.
      const currentUuidSet = new Set(allConvs.map((c) => c.uuid));
      const deletedUuids = Object.keys(seenConversations).filter((u) => !currentUuidSet.has(u));

      // Determine candidates by range + fetchAll. Then filter to only those
      // whose updated_at is newer than what we already scraped (the
      // incremental core). `onlyUuids` (retry) overrides both filters.
      const toFetch = [];
      let skippedUnchanged = 0;
      for (const c of allConvs) {
        if (onlyUuids) {
          if (!onlyUuids.includes(c.uuid)) continue;
        } else {
          if (!fetchAll) {
            const field = c[rangeField];
            if (!dateInRange(field, startDate, endDate)) continue;
          }
          const prev = seenConversations[c.uuid];
          if (prev && c.updated_at && c.updated_at <= prev) {
            skippedUnchanged++;
            continue;
          }
        }
        const project = c.project_uuid ? projectMap.get(c.project_uuid) || { uuid: c.project_uuid, name: '(unknown project)' } : null;
        toFetch.push({ project, stub: c });
      }

      if (skippedUnchanged > 0) {
        await appendLog({ level: 'ok', msg: `Skipping ${skippedUnchanged} unchanged conversations (already up to date)` });
      }
      if (deletedUuids.length > 0) {
        await appendLog({ level: 'warn', msg: `${deletedUuids.length} previously-seen conversations no longer exist on claude.ai` });
      }

      await updateCurrentRun({ total: toFetch.length });
      await appendLog({ level: 'ok', msg: `Selected ${toFetch.length} conversations to fetch` });

      const records = [];
      const seenUpdates = {};
      let completed = 0;
      let succeeded = 0;
      let failed = 0;
      let cancelled = false;
      for (const { project, stub } of toFetch) {
        if (await isCancelled()) { cancelled = true; break; }
        try {
          const full = await fetchFullConversation(orgId, stub.uuid);
          const record = buildConversationRecord(full, project);
          records.push(record);
          seenUpdates[record.uuid] = record.updated_at || stub.updated_at || null;
          succeeded++;
          await appendLog({
            level: 'ok',
            uuid: stub.uuid,
            project: project ? project.name : 'general',
            msg: stub.name || '(untitled)'
          });
        } catch (err) {
          failed++;
          await appendLog({
            level: 'error',
            uuid: stub.uuid,
            project: project ? project.name : 'general',
            msg: `${stub.name || '(untitled)'}: ${err.message}`,
            status: err.status || null,
            bodySnippet: err.bodySnippet || null
          });
        }
        completed++;
        await updateCurrentRun({ completed });
        await sleep(FETCH_DELAY_MS);
      }

      if (cancelled) await appendLog({ level: 'warn', msg: `Cancelled after ${completed}/${toFetch.length}` });

      // Build filename. "all" / "since-{YYYY-MM-DD}" for incremental-ish runs,
      // date range for explicit ranges.
      let outputFile;
      if (fetchAll) {
        outputFile = `chronicle-export-all-${new Date().toISOString().slice(0, 10)}.json`;
      } else {
        outputFile = exportFilename(startDate, endDate);
      }

      const exportObj = {
        export_metadata: {
          exported_at: new Date().toISOString(),
          range_start: fetchAll ? null : `${startDate}T00:00:00Z`,
          range_end: fetchAll ? null : `${endDate}T23:59:59Z`,
          range_field: rangeField,
          fetch_all: !!fetchAll,
          total_conversations: records.length,
          skipped_unchanged: skippedUnchanged,
          deleted_uuids: deletedUuids,
          cancelled,
          extension_version: EXTENSION_VERSION
        },
        conversations: records
      };

      // Only download if we actually fetched something or there are deletions
      // to record. Pure "nothing changed" runs produce no file.
      const noChanges = records.length === 0 && deletedUuids.length === 0 && !cancelled;
      if (!noChanges) {
        triggerDownload(JSON.stringify(exportObj, null, 2), outputFile);
      }

      // Merge freshly-seen updated_at timestamps; drop deleted UUIDs from
      // the seen map so they don't re-trigger deletion detection next run.
      const mergedSeen = { ...seenConversations, ...seenUpdates };
      for (const u of deletedUuids) delete mergedSeen[u];
      await writeState({ seenConversations: mergedSeen });

      const finishedAt = new Date().toISOString();
      const durationMs = new Date(finishedAt).getTime() - new Date(startedAt).getTime();
      await writeState({
        currentRun: null,
        lastRun: {
          startedAt,
          finishedAt,
          durationMs,
          total: toFetch.length,
          succeeded,
          failed,
          deletedCount: deletedUuids.length,
          outputFile: noChanges ? null : outputFile,
          rangeStart: fetchAll ? null : startDate,
          rangeEnd: fetchAll ? null : endDate,
          rangeField,
          fetchAll: !!fetchAll,
          noChanges,
          cancelled
        }
      });
      await clearCancelFlag();
      const summary = noChanges
        ? 'Nothing new since last run.'
        : `Export finished: ${succeeded} ok, ${failed} failed${deletedUuids.length ? `, ${deletedUuids.length} deleted` : ''} → ${outputFile}`;
      await appendLog({ level: cancelled ? 'warn' : 'ok', msg: cancelled ? `${summary} (cancelled)` : summary });
    } catch (err) {
      const finishedAt = new Date().toISOString();
      await appendLog({ level: 'error', msg: `Export aborted: ${err.message}`, status: err.status || null, bodySnippet: err.bodySnippet || null });
      await writeState({
        currentRun: null,
        lastRun: {
          startedAt,
          finishedAt,
          durationMs: new Date(finishedAt).getTime() - new Date(startedAt).getTime(),
          total: 0, succeeded: 0, failed: 0, outputFile: null, aborted: true, error: err.message
        }
      });
      await clearCancelFlag();
    }
  }

  // ──────────────────────────── message dispatch ────────────────────────────

  chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request.action === 'exportRange') {
      // Fire-and-forget; popup watches chrome.storage for updates.
      // Accept both `onlyUuids` (new) and `retryUuids` (legacy) for compatibility.
      const onlyUuids = Array.isArray(request.onlyUuids)
        ? request.onlyUuids
        : Array.isArray(request.retryUuids) ? request.retryUuids : null;
      runExport({
        fetchAll: !!request.fetchAll,
        startDate: request.startDate,
        endDate: request.endDate,
        rangeField: request.rangeField === 'created_at' ? 'created_at' : 'updated_at',
        onlyUuids
      }).catch((err) => console.error('[Chronicle] unhandled error', err));
      sendResponse({ accepted: true });
      return true;
    }
    if (request.action === 'ping') {
      sendResponse({ ok: true, version: EXTENSION_VERSION });
      return true;
    }
  });

  console.log('[Chronicle] content script loaded');
}
