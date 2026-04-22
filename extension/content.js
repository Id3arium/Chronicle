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

  async function runExport({ startDate, endDate, rangeField, onlyUuids }) {
    const startedAt = new Date().toISOString();
    await writeState({
      currentRun: { startedAt, total: 0, completed: 0, rangeStart: startDate, rangeEnd: endDate, rangeField },
      runLog: []
    });
    await appendLog({ level: 'ok', msg: `Export started (${rangeField} ${startDate} → ${endDate})` });

    try {
      const orgId = await getOrgId();
      await appendLog({ level: 'ok', msg: `Using organization ${orgId}` });

      const projects = await listProjects(orgId);
      const projectMap = new Map(projects.map((p) => [p.uuid, p]));
      await appendLog({ level: 'ok', msg: `Found ${projects.length} projects` });

      const allConvs = await listAllConversations(orgId);
      await appendLog({ level: 'ok', msg: `Found ${allConvs.length} conversations across all contexts` });

      // Filter by date range (and optional retry UUID whitelist).
      const toFetch = [];
      for (const c of allConvs) {
        const field = c[rangeField];
        if (!dateInRange(field, startDate, endDate)) continue;
        if (onlyUuids && !onlyUuids.includes(c.uuid)) continue;
        const project = c.project_uuid ? projectMap.get(c.project_uuid) || { uuid: c.project_uuid, name: '(unknown project)' } : null;
        toFetch.push({ project, stub: c });
      }

      await updateCurrentRun({ total: toFetch.length });
      await appendLog({ level: 'ok', msg: `Selected ${toFetch.length} conversations to fetch` });

      const records = [];
      const seenUpdates = {};
      let completed = 0;
      let succeeded = 0;
      let failed = 0;
      for (const { project, stub } of toFetch) {
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

      const outputFile = exportFilename(startDate, endDate);
      const exportObj = {
        export_metadata: {
          exported_at: new Date().toISOString(),
          range_start: `${startDate}T00:00:00Z`,
          range_end: `${endDate}T23:59:59Z`,
          range_field: rangeField,
          total_conversations: records.length,
          extension_version: EXTENSION_VERSION
        },
        conversations: records
      };

      triggerDownload(JSON.stringify(exportObj, null, 2), outputFile);

      // Merge freshly-seen updated_at timestamps into persistent map so future
      // "Export since last run" calls know what's already been scraped.
      const { seenConversations: prevSeen = {} } = await new Promise((resolve) =>
        chrome.storage.local.get(['seenConversations'], resolve)
      );
      const mergedSeen = { ...prevSeen, ...seenUpdates };
      await writeState({ seenConversations: mergedSeen, lastIncrementalAt: new Date().toISOString() });

      const finishedAt = new Date().toISOString();
      await writeState({
        currentRun: null,
        lastRun: {
          startedAt,
          finishedAt,
          total: toFetch.length,
          succeeded,
          failed,
          outputFile,
          rangeStart: startDate,
          rangeEnd: endDate,
          rangeField
        }
      });
      await appendLog({ level: 'ok', msg: `Export finished: ${succeeded} ok, ${failed} failed → ${outputFile}` });
    } catch (err) {
      const finishedAt = new Date().toISOString();
      await appendLog({ level: 'error', msg: `Export aborted: ${err.message}`, status: err.status || null, bodySnippet: err.bodySnippet || null });
      await writeState({
        currentRun: null,
        lastRun: { startedAt, finishedAt, total: 0, succeeded: 0, failed: 0, outputFile: null, aborted: true, error: err.message }
      });
    }
  }

  // ──────────────────────────── message dispatch ────────────────────────────

  async function runIncrementalExport() {
    // Cheap: single list call. For each conversation, compare updated_at to
    // the timestamp we last scraped for that UUID. Only fetch the delta.
    const startedAt = new Date().toISOString();
    await writeState({
      currentRun: { startedAt, total: 0, completed: 0, rangeStart: null, rangeEnd: null, rangeField: 'updated_at', incremental: true },
      runLog: []
    });
    await appendLog({ level: 'ok', msg: 'Incremental export started (since last run)' });

    try {
      const orgId = await getOrgId();
      const { seenConversations = {} } = await new Promise((resolve) =>
        chrome.storage.local.get(['seenConversations'], resolve)
      );
      const allConvs = await listAllConversations(orgId);
      await appendLog({ level: 'ok', msg: `Listed ${allConvs.length} conversations; computing delta` });

      const changed = allConvs.filter((c) => {
        const prev = seenConversations[c.uuid];
        if (!prev) return true;
        return c.updated_at && c.updated_at > prev;
      });

      if (changed.length === 0) {
        await writeState({
          currentRun: null,
          lastRun: {
            startedAt,
            finishedAt: new Date().toISOString(),
            total: 0, succeeded: 0, failed: 0, outputFile: null,
            rangeStart: null, rangeEnd: null, rangeField: 'updated_at',
            incremental: true, noChanges: true
          }
        });
        await appendLog({ level: 'ok', msg: 'Nothing new since last run.' });
        return;
      }

      // Derive a date range that covers the changed set so runExport's
      // date filter doesn't drop anything.
      const updates = changed.map((c) => c.updated_at).filter(Boolean).sort();
      const startDate = (updates[0] || new Date().toISOString()).slice(0, 10);
      const endDate = new Date().toISOString().slice(0, 10);
      const onlyUuids = changed.map((c) => c.uuid);

      await appendLog({ level: 'ok', msg: `${onlyUuids.length} changed since last run — fetching details` });
      // Hand off to runExport. It will overwrite currentRun/runLog but the
      // messages above are already in the log.
      await runExport({ startDate, endDate, rangeField: 'updated_at', onlyUuids });
    } catch (err) {
      await appendLog({ level: 'error', msg: `Incremental export aborted: ${err.message}`, status: err.status || null, bodySnippet: err.bodySnippet || null });
      await writeState({
        currentRun: null,
        lastRun: { startedAt, finishedAt: new Date().toISOString(), total: 0, succeeded: 0, failed: 0, outputFile: null, aborted: true, error: err.message, incremental: true }
      });
    }
  }

  chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request.action === 'exportRange') {
      // Fire-and-forget; popup watches chrome.storage for updates.
      // Accept both `onlyUuids` (new) and `retryUuids` (legacy) for compatibility.
      const onlyUuids = Array.isArray(request.onlyUuids)
        ? request.onlyUuids
        : Array.isArray(request.retryUuids) ? request.retryUuids : null;
      runExport({
        startDate: request.startDate,
        endDate: request.endDate,
        rangeField: request.rangeField === 'created_at' ? 'created_at' : 'updated_at',
        onlyUuids
      }).catch((err) => console.error('[Chronicle] unhandled error', err));
      sendResponse({ accepted: true });
      return true;
    }
    if (request.action === 'exportIncremental') {
      runIncrementalExport().catch((err) => console.error('[Chronicle] unhandled error', err));
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
